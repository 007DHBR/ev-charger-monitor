import os, json, sys, time, requests, warnings
from datetime import datetime, timezone, timedelta
warnings.filterwarnings('ignore')

EMAIL      = os.environ['RECHARGE_EMAIL']
PASSWORD   = os.environ['RECHARGE_PASSWORD']
NTFY_TOPIC = os.environ['NTFY_TOPIC']

STATION_ID    = 45
OWNER_ID      = 27
CHARGERS      = ['DC020', 'AC007']
STATE_FILE    = 'state.json'
POLL_INTERVAL = 30
LOOP_DURATION = 270

SL_TZ = timezone(timedelta(hours=5, minutes=30))

# If a charging session ends in less than this many seconds, it's a BMS/fault error
BMS_ERROR_THRESHOLD_SECONDS = 60   # 1 minute

API_BASES = [
    'https://recharge.lk/api',
    'https://recharge.lk:8080/api',
    'http://recharge.lk:8080/api',
]

_token = None
_base  = None

def login():
    global _token, _base
    for base in API_BASES:
        for path in ['/auth/owner/login', '/owner/authenticate', '/authenticate']:
            try:
                r = requests.post(f'{base}{path}',
                    json={'email': EMAIL, 'password': PASSWORD},
                    timeout=20, verify=False)
                if r.status_code == 200:
                    data = r.json()
                    result = data.get('result', {})
                    token = result.get('token') if isinstance(result, dict) else None
                    if token:
                        print(f'[login] OK via {base}{path}')
                        _token, _base = token, base
                        return True
            except Exception as e:
                print(f'[login] {base}{path}: {e}')
    return False

def get_charger_status():
    r = requests.get(f'{_base}/charger/getChargerStatus/{STATION_ID}',
        headers={'Authorization': f'Bearer {_token}'},
        timeout=20, verify=False)
    return r.json().get('result', []) if r.ok else []

def get_active_session(cid):
    try:
        r = requests.get(f'{_base}/oCcp/getChargerActiveSession?chargerId={cid}',
            headers={'Authorization': f'Bearer {_token}'},
            timeout=20, verify=False)
        if r.ok and r.text.strip():
            data = r.json()
            if data and isinstance(data, dict) and data.get('result'):
                return data['result'] if isinstance(data.get('result'), dict) else data
    except: pass
    return None

def extract_kwh(session):
    if not session or not isinstance(session, dict): return None
    for key in ['totalEnergy', 'energyKwh', 'energy', 'meterValue', 'kwh', 'usedEnergy']:
        v = session.get(key)
        if v is not None:
            try: return round(float(v), 2)
            except: pass
    return None

def extract_profit(session, kwh=None):
    if not session or not isinstance(session, dict): return None
    for key in ['profit', 'ownerProfit', 'netEarnings', 'netAmount', 'netProfit', 'ownerAmount']:
        v = session.get(key)
        if v is not None:
            try: return round(float(v), 2)
            except: pass
    revenue = None
    for key in ['amount', 'revenue', 'totalAmount', 'cost', 'totalCost']:
        v = session.get(key)
        if v is not None:
            try: revenue = round(float(v), 2); break
            except: pass
    ceb_cost = None
    for key in ['cebCost', 'electricityCost', 'cebAmount', 'unitCost', 'cebTotal']:
        v = session.get(key)
        if v is not None:
            try: ceb_cost = round(float(v), 2); break
            except: pass
    commission = None
    for key in ['commission', 'platformFee', 'commissionAmount', 'fee']:
        v = session.get(key)
        if v is not None:
            try: commission = round(float(v), 2); break
            except: pass
    if revenue is not None:
        deductions = 0
        if ceb_cost is not None: deductions += ceb_cost
        if commission is not None: deductions += commission
        if deductions > 0:
            return round(revenue - deductions, 2)
    return None

def notify(title, body, tags='electric_plug', priority='high'):
    try:
        r = requests.post(f'https://ntfy.sh/{NTFY_TOPIC}',
            data=body.encode('utf-8'),
            headers={'Title': title, 'Tags': tags, 'Priority': priority,
                     'Content-Type': 'text/plain; charset=utf-8'},
            timeout=15)
        print(f'[notify] {title} -> HTTP {r.status_code}')
    except Exception as e:
        print(f'[notify] error: {e}')

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f: return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=2)

def sl_now_str():
    return datetime.now(SL_TZ).strftime('%I:%M %p')

def sl_today():
    return datetime.now(SL_TZ).strftime('%Y-%m-%d')

def sl_hour():
    return datetime.now(SL_TZ).hour

def utc_now_ts():
    return datetime.now(timezone.utc).timestamp()

def ensure_daily(state):
    today = sl_today()
    if state.get('daily', {}).get('date') != today:
        state['daily'] = {
            'date': today,
            'kwh': 0.0,
            'profit': 0.0,
            'sessions': 0,
            'summary_sent': False,
        }
    return state

def check_once():
    chargers_raw = get_charger_status()
    if not chargers_raw:
        print('[check] No charger data'); return

    state = load_state()
    state = ensure_daily(state)
    now_ts = utc_now_ts()
    now_iso = datetime.now(timezone.utc).isoformat()

    for cid in CHARGERS:
        info = next((c for c in chargers_raw if c['chargerId'] == cid), None)
        if not info: continue

        connectors = info.get('connectors', [])
        def has(s): return any(c['status'] == s for c in connectors)
        if has('Charging'):       status = 'Charging'
        elif has('Preparing') or has('SuspendedEV') or has('SuspendedEVSE'): status = 'Preparing'
        elif has('Finishing'):    status = 'Finishing'
        else:                     status = 'Available'

        prev = state.get(cid, {})
        prev_status = prev.get('status', 'Unknown')
        print(f'[{cid}] {prev_status} -> {status}')

        if status == 'Charging' and prev_status != 'Charging':
            prev['charge_start_ts'] = now_ts
            print(f'[{cid}] charge_start_ts recorded')

        if status == 'Charging':
            session = get_active_session(cid)
            if session:
                kwh = extract_kwh(session)
                profit = extract_profit(session, kwh)
                if kwh is not None: prev['last_kwh'] = kwh
                if profit is not None: prev['last_profit'] = profit

        state[cid] = {**prev, 'status': status, 'updated': now_iso}

        if prev_status == 'Unknown' or prev_status == status:
            continue

        ctype = 'DC Fast' if cid.startswith('DC') else 'AC'
        time_str = sl_now_str()

        if status == 'Preparing' and prev_status not in ('Charging',):
            notify(
                title=f'\u26a1 {cid} - Vehicle Plugged In',
                body=f'A vehicle has just plugged into {ctype} charger {cid}.\nConnector is preparing — charging will start shortly.\nTime: {time_str}',
                tags='electric_plug,hourglass_flowing_sand',
                priority='high'
            )

        elif status == 'Charging' and prev_status != 'Charging':
            notify(
                title=f'\u26a1 {cid} - Charging Started',
                body=f'{ctype} charger {cid} is now actively charging.\nTime: {time_str}',
                tags='electric_plug,white_check_mark',
                priority='high'
            )

        elif status in ('Finishing', 'Available') and prev_status in ('Charging', 'Finishing', 'Preparing'):
            charge_start_ts = prev.get('charge_start_ts')
            session_duration = (now_ts - charge_start_ts) if charge_start_ts else None

            session = get_active_session(cid)
            kwh    = extract_kwh(session)    or prev.get('last_kwh')
            profit = extract_profit(session) or prev.get('last_profit')

            # BMS/fault: session ended under threshold regardless of kWh — even 0.0 kWh counts
            is_bms_error = (
                session_duration is not None and
                session_duration < BMS_ERROR_THRESHOLD_SECONDS
            )

            if is_bms_error:
                duration_str = f'{int(session_duration)}s' if session_duration else 'unknown'
                kwh_str = f'{kwh} kWh' if kwh else '0 kWh'
                notify(
                    title=f'\u26a0\ufe0f {cid} - BMS ERROR / Fault Detected',
                    body=(
                        f'WARNING: {ctype} charger {cid} stopped unexpectedly!\n'
                        f'Session lasted only {duration_str} — {kwh_str} delivered.\n'
                        f'Possible cause: Vehicle BMS error, connector fault, or charger issue.\n'
                        f'Action needed: Check the charger screen.\n'
                        f'Time: {time_str}'
                    ),
                    tags='warning,rotating_light',
                    priority='urgent'
                )
            else:
                lines = [f'{ctype} charger {cid}: charging session complete.']
                if kwh is not None:    lines.append(f'Energy delivered: {kwh} kWh')
                if profit is not None: lines.append(f'Profit earned:    Rs {profit}')
                if kwh is None and profit is None:
                    lines.append('(Session data not available from API)')
                lines.append(f'Time: {time_str}')

                notify(
                    title=f'\u2705 {cid} - Charging Complete',
                    body=chr(10).join(lines),
                    tags='battery,moneybag'
                )

                if kwh:    state['daily']['kwh']    = round(state['daily']['kwh'] + kwh, 2)
                if profit: state['daily']['profit'] = round(state['daily']['profit'] + profit, 2)
                state['daily']['sessions'] += 1

            state[cid].pop('last_kwh', None)
            state[cid].pop('last_profit', None)
            state[cid].pop('charge_start_ts', None)

        elif status == 'Available' and prev_status == 'Preparing':
            notify(
                title=f'{cid} - Vehicle Disconnected',
                body=f'{ctype} charger {cid} is now free.\nVehicle unplugged before charging started.\nTime: {time_str}',
                priority='default', tags='wave'
            )

    if sl_hour() == 21 and not state['daily'].get('summary_sent'):
        d = state['daily']
        today_str = datetime.now(SL_TZ).strftime('%d %b %Y')
        lines = [
            f'Daily summary for {today_str}',
            f'Total sessions:  {d["sessions"]}',
            f'Total energy:    {d["kwh"]} kWh',
            f'Total profit:    Rs {d["profit"]}',
        ]
        notify(
            title=f'Daily Report - {today_str}',
            body=chr(10).join(lines),
            tags='bar_chart,moneybag',
            priority='default'
        )
        state['daily']['summary_sent'] = True

    save_state(state)

def main():
    if not login():
        print('[main] Login failed'); sys.exit(1)

    start = time.time()
    iteration = 0

    while True:
        iteration += 1
        elapsed = time.time() - start
        print(f'--- Check #{iteration} (elapsed {int(elapsed)}s) [{datetime.now(SL_TZ).strftime("%H:%M:%S")} SL] ---')

        try:
            check_once()
        except Exception as e:
            print(f'[check] Error: {e}')
            if not login():
                print('[main] Re-login failed, stopping'); break

        elapsed = time.time() - start
        if elapsed >= LOOP_DURATION:
            print(f'[main] Done ({int(elapsed)}s)'); break

        sleep_time = POLL_INTERVAL - (time.time() - start - (iteration - 1) * POLL_INTERVAL)
        if sleep_time > 0:
            print(f'[main] Sleeping {int(sleep_time)}s...')
            time.sleep(sleep_time)

if __name__ == '__main__':
    main()
