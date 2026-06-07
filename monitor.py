import os, json, sys, requests
from datetime import datetime, timezone

EMAIL       = os.environ['RECHARGE_EMAIL']
PASSWORD    = os.environ['RECHARGE_PASSWORD']
NTFY_TOPIC  = os.environ['NTFY_TOPIC']

API_BASE   = 'https://recharge.lk:8080/api'
STATION_ID = 45
CHARGERS   = ['DC020', 'AC007']
STATE_FILE = 'state.json'

def login():
    for path in ['/auth/owner/login', '/owner/authenticate', '/authenticate']:
        try:
            r = requests.post(f'{API_BASE}{path}', json={'email': EMAIL, 'password': PASSWORD}, timeout=15)
            if r.status_code == 200:
                data = r.json()
                result = data.get('result', {})
                token = result.get('token') if isinstance(result, dict) else None
                if token:
                    print(f'[login] OK via {path}')
                    return token
        except Exception as e:
            print(f'[login] {path} error: {e}')
    return None

def get_charger_status(token):
    r = requests.get(f'{API_BASE}/charger/getChargerStatus/{STATION_ID}',
        headers={'Authorization': f'Bearer {token}'}, timeout=15)
    return r.json().get('result', []) if r.ok else []

def get_active_session(cid, token):
    try:
        r = requests.get(f'{API_BASE}/oCcp/getChargerActiveSession?chargerId={cid}',
            headers={'Authorization': f'Bearer {token}'}, timeout=15)
        if r.ok and r.text.strip():
            return r.json()
    except: pass
    return None

def notify(title, body, tags='electric_plug', priority='high'):
    try:
        requests.post(f'https://ntfy.sh/{NTFY_TOPIC}', data=body.encode(),
            headers={'Title': title, 'Tags': tags, 'Priority': priority}, timeout=15)
        print(f'[notify] {title}')
    except Exception as e:
        print(f'[notify] error: {e}')

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f: return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=2)

def main():
    token = login()
    if not token: sys.exit(1)
    chargers_raw = get_charger_status(token)
    if not chargers_raw: sys.exit(1)
    prev = load_state()
    new_state = {}
    now = datetime.now(timezone.utc).isoformat()
    for cid in CHARGERS:
        info = next((c for c in chargers_raw if c['chargerId'] == cid), None)
        if not info: continue
        connectors = info.get('connectors', [])
        def has(s): return any(c['status'] == s for c in connectors)
        if has('Charging'): status = 'Charging'
        elif has('Preparing') or has('SuspendedEV'): status = 'Preparing'
        elif has('Finishing'): status = 'Finishing'
        else: status = 'Available'
        session = get_active_session(cid, token)
        new_state[cid] = {'status': status, 'updated': now}
        prev_status = prev.get(cid, {}).get('status', 'Unknown')
        print(f'[{cid}] {prev_status} -> {status}')
        if prev_status == 'Unknown' or prev_status == status: continue
        ctype = 'DC Fast' if cid.startswith('DC') else 'AC'
        if status in ('Preparing', 'Charging'):
            notify(f'EV {cid} - Vehicle plugged in', f'{ctype} charger {cid}: vehicle connected and {status.lower()}.')
        elif status == 'Finishing' or (prev_status == 'Charging' and status == 'Available'):
            lines = [f'{ctype} charger {cid}: session complete.']
            if session and isinstance(session, dict):
                kwh = session.get('totalEnergy') or session.get('energyKwh') or session.get('energy')
                rev = session.get('amount') or session.get('revenue') or session.get('totalAmount')
                if kwh: lines.append(f'Energy: {kwh} kWh')
                if rev: lines.append(f'Revenue: Rs {rev}')
            notify(f'DONE {cid} - Charging complete', chr(10).join(lines), tags='battery,moneybag')
        elif status == 'Available':
            notify(f'{cid} - Vehicle disconnected', f'{ctype} charger {cid} is now free.', priority='default')
    save_state(new_state)

if __name__ == '__main__':
    main()
