import os, json, sys, time, requests, warnings
from datetime import datetime, timezone, timedelta
warnings.filterwarnings('ignore')

EMAIL = os.environ['RECHARGE_EMAIL']
PASSWORD = os.environ['RECHARGE_PASSWORD']
NTFY_TOPIC = os.environ['NTFY_TOPIC']

STATION_ID = 45
OWNER_ID = 27
CHARGERS = ['DC020', 'AC007']
STATE_FILE = 'state.json'
POLL_INTERVAL = 30

SL_TZ = timezone(timedelta(hours=5, minutes=30))

BMS_ERROR_THRESHOLD_SECONDS = 60
STUCK_PREPARING_THRESHOLD_SECONDS = 300

API_BASES = [
    'https://recharge.lk/api',
    'https://recharge.lk:8080/api',
    'http://recharge.lk:8080/api',
]

_token = None
_base = None

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
                        print(f'[login] OK via {base}{path}', flush=True)
                        _token, _base = token, base
                        return True
            except Exception as e:
                print(f'[login] {base}{path}: {e}', flush=True)
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

def get_today_profit():
    today = sl_today()
    bases = ['https://recharge.lk:8080/api', 'http://recharge.lk:8080/api', _base]
    for base in bases:
        try:
            r = requests.get(
                f'{base}/owner/getAllSessionHistory/{OWNER_ID}',
                params={'startDate': today, 'endDate': today},
                headers={'Authorization': f'Bearer {_token}'},
                timeout=20, verify=False
            )
            if not r.ok:
                continue
            data = r.json()
            items = data.get('result', [])
            if not items:
                return (0.0, 0.0, 0)
            total_profit = 0.0
            total_kwh = 0.0
            total_sessions = 0
            for station in items:
                for session in station.get('walletData', []):
                    start = session.get('start', '')
                    if start.startswith(today):
                        total_profit += float(session.get('profit', 0) or 0)
                        total_kwh += float(session.get('usedKwh', 0) or 0)
                        total_sessions += 1
                for session in station.get('packageData', []):
                    start = session.get('start', '')
                    if start.startswith(today):
                        total_profit += float(session.get('profit', 0) or 0)
                        total_kwh += float(session.get('usedKwh', 0) or 0)
                        total_sessions += 1
            print(f'[profit] Today: {total_sessions} sessions, {round(total_kwh,2)} kWh, Rs {round(total_profit,2)}', flush=True)
            return (round(total_profit, 2), round(total_kwh, 3), total_sessions)
        except Exception as e:
            print(f'[profit] {base}: {e}', flush=True)
    print('[profit] All bases failed \u2014 using accumulated state', flush=True)
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
        print(f'[notify] {title} -> HTTP {r.status_code}', flush=True)
    except Exception as e:
        print(f'[notify] error: {e}', flush=True)

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
            'heartbeat_sent': False,
            'target_5000_sent': False,
            'target_10000_sent': False,
        }
    return state

def ensure_weekly(state):
    now = datetime.now(SL_TZ)
    monday = (now - timedelta(days=now.weekday())).strftime('%Y-%m-%d')
    if state.get('weekly', {}).get('week_start') != monday:
        state['weekly'] = {
            'week_start': monday,
            'kwh': 0.0,
            'profit': 0.0,
            'sessions': 0,
            'summary_sent': False,
        }
    return state

def check_once():
    chargers_raw = get_charger_status()
    if not chargers_raw:
        print('[check] No charger data', flush=True); return

    state = load_state()
    state = ensure_daily(state)
    state = ensure_weekly(state)
    now_ts = utc_now_ts()
    now_iso = datetime.now(timezone.utc).isoformat()

    for cid in CHARGERS:
        info = next((c for c in chargers_raw if c['chargerId'] == cid), None)
        if not info:
            prev = state.get(cid, {})
            miss_count = prev.get('missing_count', 0) + 1
            state[cid] = {**prev, 'missing_count': miss_count}
            if miss_count == 2:
                ctype = 'DC Fast' if cid.startswith('DC') else 'AC'
                notify(
                    title=f'\u26ab {cid} - Charger Offline',
                    body=f'{ctype} charger {cid} is not responding. May have lost power or internet.',
                    tags='warning,no_entry',
                    priority='high'
                )
            save_state(state)
            continue
        prev_missing = state.get(cid, {}).get('missing_count', 0)
        if prev_missing >= 2:
            ctype = 'DC Fast' if cid.startswith('DC') else 'AC'
            notify(
                title=f'\u2705 {cid} - Charger Back Online',
                body=f'{ctype} charger {cid} is back online.',
                tags='white_check_mark',
                priority='default'
            )
        if cid in state: state[cid]['missing_count'] = 0

        connectors = info.get('connectors', [])
        def has(s): return any(c['status'] == s for c in connectors)
        if has('Charging'): status = 'Charging'
        elif has('Preparing') or has('SuspendedEV') or has('SuspendedEVSE'): status = 'Preparing'
        elif has('Finishing'): status = 'Finishing'
        else: status = 'Available'

        prev = state.get(cid, {})
        prev_status = prev.get('status', 'Unknown')
        print(f'[{cid}] {prev_status} -> {status}', flush=True)

        if status == 'Charging' and prev_status != 'Charging':
            prev['charge_start_ts'] = now_ts

        if status == 'Charging':
            session = get_active_session(cid)
            if session:
                kwh = extract_kwh(session)
                profit = extract_profit(session, kwh)
                if kwh is not None: prev['last_kwh'] = kwh
                if profit is not None: prev['last_profit'] = profit

        state[cid] = {**prev, 'status': status, 'updated': now_iso}

        if status == 'Preparing' and not state[cid].get('preparing_since'):
            state[cid]['preparing_since'] = now_ts
        if status != 'Preparing':
            state[cid].pop('preparing_since', None)
            state[cid].pop('preparing_alert_sent', None)
        if status == 'Preparing':
            p_since = state[cid].get('preparing_since')
            if p_since and not state[cid].get('preparing_alert_sent'):
                if (now_ts - p_since) >= STUCK_PREPARING_THRESHOLD_SECONDS:
                    ctype = 'DC Fast' if cid.startswith('DC') else 'AC'
                    notify(
                        title=f'\u26a0\ufe0f {cid} - Not Charging',
                        body=f'{ctype} charger {cid} still did not charge after 5 minutes of being plugged in.',
                        tags='warning,hourglass',
                        priority='high'
                    )
                    state[cid]['preparing_alert_sent'] = True

        if prev_status == 'Unknown' or prev_status == status:
            continue

        ctype = 'DC Fast' if cid.startswith('DC') else 'AC'
        time_str = sl_now_str()

        if status == 'Preparing' and prev_status not in ('Charging',):
            notify(
                title=f'\u26a1 {cid} - Vehicle Plugged In',
                body=f'A vehicle has just plugged into {ctype} charger {cid}.\nConnector is preparing \u2014 charging will start shortly.\nTime: {time_str}',
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
            kwh = extract_kwh(session) or prev.get('last_kwh')
            profit = extract_profit(session) or prev.get('last_profit')
            is_bms_error = (session_duration is not None and session_duration < BMS_ERROR_THRESHOLD_SECONDS)
            if is_bms_error:
                notify(
                    title=f'\u26a0\ufe0f {cid} - BMS ERROR',
                    body=f'{ctype} charger {cid} stopped. BMS error detected.',
                    tags='warning,rotating_light',
                    priority='urgent'
                )
            else:
                lines = [f'{ctype} charger {cid}: charging session complete.']
                if kwh is not None: lines.append(f'Energy delivered: {kwh} kWh')
                if profit is not None: lines.append(f'Profit earned: Rs {profit}')
                if kwh is None and profit is None:
                    lines.append('(Session data not available from API)')
                lines.append(f'Time: {time_str}')
                notify(
                    title=f'\u2705 {cid} - Charging Complete',
                    body=chr(10).join(lines),
                    tags='battery,moneybag'
                )
            state[cid].pop('last_kwh', None)
            state[cid].pop('last_profit', None)
            state[cid].pop('charge_start_ts', None)

        elif status == 'Available' and prev_status == 'Preparing':
            notify(
                title=f'{cid} - Vehicle Disconnected',
                body=f'{ctype} charger {cid} is now free.\nVehicle unplugged before charging started.\nTime: {time_str}',
                priority='default', tags='wave'
            )

    profit_data = get_today_profit()
    if profit_data is not None:
        real_profit, real_kwh, real_sessions = profit_data
        prev_profit = state['daily']['profit']
        state['daily']['profit'] = real_profit
        state['daily']['kwh'] = real_kwh
        state['daily']['sessions'] = real_sessions
        delta = round(real_profit - prev_profit, 2)
        if delta > 0:
            state['weekly']['profit'] = round(state['weekly'].get('profit', 0) + delta, 2)

    daily_profit = state['daily']['profit']
    if not state['daily'].get('target_5000_sent') and daily_profit >= 5000:
        notify(
            title='\U0001f3af Daily Target \u2014 Rs. 5,000!',
            body=f"Today's profit has reached Rs. 5,000!\nTotal so far: Rs. {daily_profit}",
            tags='dart,moneybag', priority='high'
        )
        state['daily']['target_5000_sent'] = True
    if not state['daily'].get('target_10000_sent') and daily_profit >= 10000:
        notify(
            title='\U0001f525 Daily Target \u2014 Rs. 10,000!',
            body=f"Incredible! Daily profit has reached Rs. 10,000!\nTotal today: Rs. {daily_profit}",
            tags='fire,moneybag', priority='urgent'
        )
        state['daily']['target_10000_sent'] = True

    if sl_hour() == 8 and not state['daily'].get('heartbeat_sent'):
        notify(
            title='\u2705 Monitor Running',
            body='EV charger monitor is active. Both chargers are being watched.',
            tags='white_check_mark', priority='default'
        )
        state['daily']['heartbeat_sent'] = True

    if datetime.now(SL_TZ).weekday() == 0 and sl_hour() == 9 and not state['weekly'].get('summary_sent'):
        w = state['weekly']
        notify(
            title='\U0001f4ca Weekly Summary',
            body=f"Week of {w.get('week_start','this week')}\nSessions: {w['sessions']}\nEnergy: {w['kwh']} kWh\nProfit: Rs. {w['profit']}",
            tags='bar_chart,moneybag', priority='default'
        )
        state['weekly']['summary_sent'] = True

    if sl_hour() == 21 and not state['daily'].get('summary_sent'):
        d = state['daily']
        today_str = datetime.now(SL_TZ).strftime('%d %b %Y')
        notify(
            title=f'Daily Report - {today_str}',
            body=f"Daily summary for {today_str}\nTotal sessions: {d['sessions']}\nTotal energy: {d['kwh']} kWh\nTotal profit: Rs {d['profit']}",
            tags='bar_chart,moneybag', priority='default'
        )
        state['daily']['summary_sent'] = True

    save_state(state)

def main():
    print('[main] EV Charger Monitor starting...', flush=True)
    # Keep retrying login until it succeeds
    while not login():
        print('[main] Login failed, retrying in 60s...', flush=True)
        time.sleep(60)

    iteration = 0
    last_login = time.time()

    while True:
        iteration += 1
        print(f'--- Poll #{iteration} [{datetime.now(SL_TZ).strftime("%H:%M:%S")} SL] ---', flush=True)

        try:
            check_once()
        except Exception as e:
            print(f'[check] Error: {e}', flush=True)
            if not login():
                print('[main] Re-login failed, waiting 60s...', flush=True)
                time.sleep(60)
                continue

        # Re-login every 6 hours to keep token fresh
        if time.time() - last_login > 21600:
            print('[main] Refreshing token...', flush=True)
            if login():
                last_login = time.time()

        time.sleep(POLL_INTERVAL)

if __name__ == '__main__':
    main()
