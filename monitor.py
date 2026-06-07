import os, json, sys, requests, warnings
from datetime import datetime, timezone
warnings.filterwarnings('ignore')  # suppress SSL warnings

EMAIL       = os.environ['RECHARGE_EMAIL']
PASSWORD    = os.environ['RECHARGE_PASSWORD']
NTFY_TOPIC  = os.environ['NTFY_TOPIC']

STATION_ID = 45
CHARGERS   = ['DC020', 'AC007']
STATE_FILE = 'state.json'

# Try multiple base URLs - port 443 first, then 8080
API_BASES = [
    'https://recharge.lk/api',
    'https://recharge.lk:8080/api',
    'http://recharge.lk:8080/api',
]

def login():
    login_paths = ['/auth/owner/login', '/owner/authenticate', '/authenticate']
    for base in API_BASES:
        for path in login_paths:
            try:
                r = requests.post(
                    f'{base}{path}',
                    json={'email': EMAIL, 'password': PASSWORD},
                    timeout=20,
                    verify=False
                )
                if r.status_code == 200:
                    data = r.json()
                    result = data.get('result', {})
                    token = result.get('token') if isinstance(result, dict) else None
                    if token:
                        print(f'[login] OK via {base}{path}')
                        return token, base
            except Exception as e:
                print(f'[login] {base}{path} error: {e}')
    return None, None

def get_charger_status(base, token):
    r = requests.get(
        f'{base}/charger/getChargerStatus/{STATION_ID}',
        headers={'Authorization': f'Bearer {token}'},
        timeout=20, verify=False
    )
    if r.ok:
        return r.json().get('result', [])
    print(f'[status] HTTP {r.status_code}')
    return []

def get_active_session(base, cid, token):
    try:
        r = requests.get(
            f'{base}/oCcp/getChargerActiveSession?chargerId={cid}',
            headers={'Authorization': f'Bearer {token}'},
            timeout=20, verify=False
        )
        if r.ok and r.text.strip():
            return r.json()
    except: pass
    return None

def notify(title, body, tags='electric_plug', priority='high'):
    try:
        r = requests.post(
            f'https://ntfy.sh/{NTFY_TOPIC}',
            data=body.encode('utf-8'),
            headers={
                'Title': title,
                'Tags': tags,
                'Priority': priority,
                'Content-Type': 'text/plain; charset=utf-8',
            },
            timeout=15
        )
        print(f'[notify] {title} -> HTTP {r.status_code}')
    except Exception as e:
        print(f'[notify] error: {e}')

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f: return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=2)

def main():
    token, base = login()
    if not token:
        print('[main] Login failed on all endpoints')
        sys.exit(1)

    chargers_raw = get_charger_status(base, token)
    if not chargers_raw:
        print('[main] No charger data returned')
        sys.exit(1)

    prev = load_state()
    new_state = {}
    now = datetime.now(timezone.utc).isoformat()

    for cid in CHARGERS:
        info = next((c for c in chargers_raw if c['chargerId'] == cid), None)
        if not info:
            print(f'[main] {cid} not found')
            continue

        connectors = info.get('connectors', [])
        def has(s): return any(c['status'] == s for c in connectors)
        if has('Charging'): status = 'Charging'
        elif has('Preparing') or has('SuspendedEV') or has('SuspendedEVSE'): status = 'Preparing'
        elif has('Finishing'): status = 'Finishing'
        else: status = 'Available'

        session = get_active_session(base, cid, token)
        new_state[cid] = {'status': status, 'updated': now}

        prev_status = prev.get(cid, {}).get('status', 'Unknown')
        print(f'[{cid}] prev={prev_status}  curr={status}')

        if prev_status == 'Unknown' or prev_status == status:
            continue

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
            notify(
                title=f'DONE {cid} - Charging complete',
                body=chr(10).join(lines),
                tags='battery,moneybag'
            )
        elif status == 'Available':
            notify(
                title=f'{cid} - Vehicle disconnected',
                body=f'{ctype} charger {cid} is now free.',
                priority='default'
            )

    save_state(new_state)

if __name__ == '__main__':
    main()
