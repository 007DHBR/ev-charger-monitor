import os, json, sys, time, requests, warnings
from datetime import datetime, timezone
warnings.filterwarnings('ignore')

EMAIL      = os.environ['RECHARGE_EMAIL']
PASSWORD   = os.environ['RECHARGE_PASSWORD']
NTFY_TOPIC = os.environ['NTFY_TOPIC']

STATION_ID   = 45
CHARGERS     = ['DC020', 'AC007']
STATE_FILE   = 'state.json'
POLL_INTERVAL = 30   # seconds between checks
LOOP_DURATION = 270  # run for 4.5 min, then exit so next cron can take over

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
            return r.json()
    except: pass
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

def check_once():
    chargers_raw = get_charger_status()
    if not chargers_raw:
        print('[check] No charger data')
        return

    prev = load_state()
    new_state = {}
    now = datetime.now(timezone.utc).isoformat()

    for cid in CHARGERS:
        info = next((c for c in chargers_raw if c['chargerId'] == cid), None)
        if not info: continue

        connectors = info.get('connectors', [])
        def has(s): return any(c['status'] == s for c in connectors)
        if has('Charging'): status = 'Charging'
        elif has('Preparing') or has('SuspendedEV') or has('SuspendedEVSE'): status = 'Preparing'
        elif has('Finishing'): status = 'Finishing'
        else: status = 'Available'

        new_state[cid] = {'status': status, 'updated': now}
        prev_status = prev.get(cid, {}).get('status', 'Unknown')
        print(f'[{cid}] {prev_status} -> {status}')

        if prev_status == 'Unknown' or prev_status == status:
            continue

        session = get_active_session(cid)
        ctype = 'DC Fast' if cid.startswith('DC') else 'AC'

        if status in ('Preparing', 'Charging'):
            notify(
                title=f'EV {cid} - Vehicle plugged in',
                body=f'{ctype} charger {cid}: vehicle connected and {status.lower()}.',
                tags='electric_plug,white_check_mark'
            )
        elif status == 'Finishing' or (prev_status == 'Charging' and status == 'Available'):
            lines = [f'{ctype} charger {cid}: session complete.']
            if session and isinstance(session, dict):
                kwh = session.get('totalEnergy') or session.get('energyKwh') or session.get('energy')
                rev = session.get('amount') or session.get('revenue') or session.get('totalAmount')
                if kwh: lines.append(f'Energy: {kwh} kWh')
                if rev: lines.append(f'Revenue: Rs {rev}')
            notify(title=f'DONE {cid} - Charging complete',
                   body=chr(10).join(lines), tags='battery,moneybag')
        elif status == 'Available':
            notify(title=f'{cid} - Vehicle disconnected',
                   body=f'{ctype} charger {cid} is now free.', priority='default')

    save_state(new_state)

def main():
    if not login():
        print('[main] Login failed on all endpoints')
        sys.exit(1)

    start = time.time()
    iteration = 0

    while True:
        iteration += 1
        elapsed = time.time() - start
        print(f'--- Check #{iteration} (elapsed {int(elapsed)}s) ---')

        try:
            check_once()
        except Exception as e:
            print(f'[check] Error: {e}')
            # Re-login on error
            if not login():
                print('[main] Re-login failed, stopping')
                break

        elapsed = time.time() - start
        if elapsed >= LOOP_DURATION:
            print(f'[main] Loop duration reached ({int(elapsed)}s), exiting')
            break

        sleep_time = POLL_INTERVAL - (time.time() - start - (iteration - 1) * POLL_INTERVAL)
        if sleep_time > 0:
            print(f'[main] Sleeping {int(sleep_time)}s...')
            time.sleep(sleep_time)

if __name__ == '__main__':
    main()
