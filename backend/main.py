# backend/main.py
from .api import app, run_server
from flask import Flask, request, jsonify, render_template
import re

@app.route('/errors')
def errors_page():
    from .error_logger import (
        get_available_log_dates,
        set_last_errors_visit,
        get_errors_for_date,
        get_last_errors_visit
    )
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        PARIS_TZ = ZoneInfo("Europe/Paris")
    except ImportError:
        PARIS_TZ = None

    # Step 1: Get last visit BEFORE marking as read
    last_visit_str = get_last_errors_visit()

    # Safe parser for ISO timestamps
    def parse_iso_utc(s):
        s = s.strip()
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        if '.' in s:
            date_part, subsec_tz = s.split('.', 1)
            subsec = subsec_tz.split('+', 1)[0].split('Z')[0]
            tz_part = '+' + subsec_tz.split('+', 1)[1] if '+' in subsec_tz else '+00:00'
            subsec = subsec[:6]
            s = f"{date_part}.{subsec}{tz_part}"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    try:
        last_visit_dt = parse_iso_utc(last_visit_str)
    except:
        last_visit_dt = datetime(1970, 1, 1, tzinfo=timezone.utc)

    # Load errors
    now_paris = datetime.now(PARIS_TZ) if PARIS_TZ else datetime.now()
    today = now_paris.strftime("%Y-%m-%d")
    errors = get_errors_for_date(today)
    log_dates = get_available_log_dates()

    # Annotate errors with is_new and French display time
    for err in errors:
        try:
            err_dt_utc = parse_iso_utc(err['timestamp'])
            # Convert to Paris time for display
            if PARIS_TZ:
                err_paris = err_dt_utc.astimezone(PARIS_TZ)
            else:
                err_paris = err_dt_utc.astimezone()
            err['display_time'] = err_paris.strftime("%d/%m/%Y %H:%M:%S")
            err['is_new'] = err_dt_utc > last_visit_dt
        except Exception:
            err['display_time'] = err['timestamp']
            err['is_new'] = False

    # Step 2: NOW mark as read
    set_last_errors_visit()

    return render_template(
        'errors.html',
        errors=errors,
        log_dates=log_dates,
        current_date=today
    )

# In backend/api.py
@app.route('/api/errors/<date_str>')
def api_errors_by_date(date_str):
    import re
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return jsonify({'error': 'Invalid date'}), 400
    try:
        from datetime import datetime
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({'error': 'Invalid date'}), 400

    from .error_logger import get_errors_for_date
    errors = get_errors_for_date(date_str)
    
    # Format display_time in French (match main.py logic)
    try:
        from zoneinfo import ZoneInfo
        PARIS_TZ = ZoneInfo("Europe/Paris")
    except ImportError:
        PARIS_TZ = None

    from datetime import datetime, timezone
    def parse_iso_utc(s):
        s = s.strip()
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        if '.' in s:
            date_part, subsec_tz = s.split('.', 1)
            subsec = subsec_tz.split('+', 1)[0].split('Z')[0]
            tz_part = '+' + subsec_tz.split('+', 1)[1] if '+' in subsec_tz else '+00:00'
            subsec = subsec[:6]
            s = f"{date_part}.{subsec}{tz_part}"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    for err in errors:
        try:
            err_dt_utc = parse_iso_utc(err['timestamp'])
            if PARIS_TZ:
                err_paris = err_dt_utc.astimezone(PARIS_TZ)
            else:
                err_paris = err_dt_utc.astimezone()
            err['display_time'] = err_paris.strftime("%d/%m/%Y %H:%M:%S")
        except:
            err['display_time'] = err['timestamp']
        err['is_new'] = False  # logs are historical — never "new"

    return jsonify({'errors': errors})
    
if __name__ == '__main__':
    run_server() 