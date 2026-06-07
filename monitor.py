import os, json, sys, time, requests, warnings
from datetime import datetime, timezone, timedelta
warnings.filterwarnings('ignore')

EMAIL      = os.environ['RECHARGE_EMAIL']
PASSWORD   = os.environ['RECHARGE_PASSWORD']
NTFY_TOPIC = os.environ['NTFY_TOPIC']

STATION_ID    = 45
CHARGERS      = ['DC020', 'AC007']
STATE_FILE    = 'state.json'
POLL_INTERVAL = 30    # seconds between checks
LOOP_DURATION = 270   # 4.5 min loop, then exit for next cron

# Sri Lanka is UTC+5:30
SL_TZ = timezone(timedelta(hours=5, minutes=30))

API_BASES = [
    'https://recharge.lk/api',
    'https://recharge.lk:8080/api',
    'http://recharge.lk:8080/api',
]

_token = None
_base  = None

# ── Auth ─────────────────────────────────────────────────────────────────────

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

# ── API ───────────────────────────────────────────────────────────────────────

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
            if data and isinstance(data, dict):
                return data
    except: pass
    return None

# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_kwh(session):
    if not session or not isinstance(session, dict): return None
    for key in ['totalEnergy', 'energyKwh', 'energy', 'meterValue', 'kwh']:
        v = session.get(key)
        if v is not None:
            try: return round(float(v), 2)
            except: pass
    return None

def extract_revenue(session):
    if not session or not isinstance(session, dict): return None
    for key in ['amount', 'revenue', 'totalAmount', 'cost', 'price']:
        v = session.get(key)
        if v is not None:
            try: return round(float(v), 2)
            except: pass
    return None

# ── Notifications ─────────────────────────────────────────────────────────────

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

# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f: return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=2)

def sl_today():
    return datetime.now(SL_TZ).strftime('%Y-%m-%d')

def sl_hour():
    return datetime.now(SL_TZ).hour

def ensure_daily(state):
    today = sl_today()
    if state.get('daily', {}).get('date') != today:
        state['daily'] = {
            'date': today,
            'kwh': 0.0,
            'revenue': 0.0,
            'sessions': 0,
            'summary_sent': False,
        }
    return state

# ── Core check ───────────────────────────────────────────────────────────────

def check_once():
    chargers_raw = get_charger_status()
    if not chargers_raw:
        print('[check] No charger data'); return

    state = load_state()
    state = ensure_daily(state)
    now = datetime.now(timezone.utc).isoformat()

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

        # While charging, keep refreshing session snapshot so we have data when it ends
        if status == 'Charging':
            session = get_active_session(cid)
            if session:
                kwh = extract_kwh(session)
                rev = extract_revenue(session)
                if kwh is not None: prev['last_kwh'] = kwh
                if rev is not None: prev['last_rev'] = rev

        state[cid] = {**prev, 'status': status, 'updated': now}

        if prev_status == 'Unknown' or prev_status == status:
            continue

        ctype = 'DC Fast' if cid.startswith('DC') else 'AC'

        # ── Plug-in / start ───────────────────────────────────────────────
        if status in ('Preparing', 'Charging'):
            action = 'started charging' if status == 'Charging' else 'plugged in'
            notify(
                title=f'EV {cid} - Vehicle {action}',
                body=f'{ctype} charger {cid}: vehicle connected and {action}.',
                tags='electric_plug,white_check_mark'
            )

        # ── Session complete ──────────────────────────────────────────────
        elif status in ('Finishing', 'Available') and prev_status in ('Charging', 'Finishing', 'Preparing'):
            # Try live session one more time; fall back to last snapshot
            session = get_active_session(cid)
            kwh = extract_kwh(session) or prev.get('last_kwh')
            rev = extract_revenue(session) or prev.get('last_rev')

            lines = [f'{ctype} charger {cid}: charging session complete.']
            if kwh is not None: lines.append(f'Energy delivered: {kwh} kWh')
            if rev is not None: lines.append(f'Revenue earned:   Rs {rev}')
            if kwh is None and rev is None:
                lines.append('(Session data not available from API)')

            notify(
                title=f'DONE {cid} - Charging complete',
                body=chr(10).join(lines),
                tags='battery,moneybag'
            )

            # Add to daily totals
            if kwh: state['daily']['kwh']     = round(state['daily']['kwh'] + kwh, 2)
            if rev: state['daily']['revenue'] = round(state['daily']['revenue'] + rev, 2)
            state['daily']['sessions'] += 1

            # Clear snapshot
            state[cid].pop('last_kwh', None)
            state[cid].pop('last_rev', None)

        # ── Disconnected (not after charging) ────────────────────────────
        elif status == 'Available' and prev_status == 'Preparing':
            notify(
                title=f'{cid} - Vehicle disconnected',
                body=f'{ctype} charger {cid} is now free (unplugged without charging).',
                priority='default', tags='wave'
            )

    # ── 9pm daily summary (Sri Lanka time) ───────────────────────────────────
    if sl_hour() == 21 and not state['daily'].get('summary_sent'):
        d = state['daily']
        today_str = datetime.now(SL_TZ).strftime('%d %b %Y')
        lines = [
            f'Daily summary for {today_str}',
            f'Total sessions:  {d["sessions"]}',
            f'Total energy:    {d["kwh"]} kWh',
            f'Total revenue:   Rs {d["revenue"]}',
        ]
        notify(
            title=f'Daily Report - {today_str}',
            body=chr(10).join(lines),
            tags='bar_chart,moneybag',
            priority='default'
        )
        state['daily']['summary_sent'] = True

    save_state(state)

# ── Main loop ─────────────────────────────────────────────────────────────────

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
