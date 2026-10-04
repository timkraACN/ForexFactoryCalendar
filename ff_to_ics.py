#!/usr/bin/env python3
"""
ForexFactory -> Apple Kalender (als abonnierbare ICS-Datei)

- Holt die Wochentermine aus dem offiziellen FF-JSON-Feed (Titel, Währung, Zeit, Impact, Prognose, Vorwert)
- Versucht nach der Veröffentlichung das tatsächliche Ergebnis ("Actual") von forexfactory.com zu lesen
- Merkt sich alles in data/events.json, damit vergangene Termine samt Ergebnis im Kalender bleiben
- Schreibt docs/forexfactory.ics, die Apple Kalender abonniert
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------- Einstellungen ----------------
FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STATE = Path("data/events.json")
OUT = Path("docs/forexfactory.ics")

IMPACTS = {"High", "Medium"}
CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"}          
EVENT_MINUTES = 15          # Dauer des Kalendereintrags
ALARM_MINUTES = 10          # Erinnerung vorher (0 = keine)
KEEP_DAYS = 90              # wie lange alte Termine im Kalender bleiben
# -----------------------------------------------

ICON = {"High": "🔴", "Medium": "🟠", "Low": "🟡"}
TONE = {"better": "🟢", "worse": "🔻"}


def uid(ev):
    raw = f"{ev['title']}|{ev['country']}|{ev['date']}"
    return hashlib.sha1(raw.encode()).hexdigest() + "@ff-kalender"


def wanted(ev):
    if ev.get("impact") not in IMPACTS:
        return False
    if CURRENCIES and ev.get("country") not in CURRENCIES:
        return False
    return bool(ev.get("date"))


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def fetch_feed():
    r = requests.get(FEED, timeout=30, headers={"User-Agent": "ff-kalender/1.0"})
    r.raise_for_status()
    return r.json()


def merge(state, feed):
    """Neue/aktualisierte Termine übernehmen, verschobene/gestrichene entfernen."""
    feed_keys = set()
    dates = []
    for ev in feed:
        if not ev.get("date"):
            continue
        dates.append(datetime.fromisoformat(ev["date"]))
        if not wanted(ev):
            continue
        k = uid(ev)
        feed_keys.add(k)
        old = state.get(k, {})
        state[k] = {
            **old,
            "title": ev["title"],
            "country": ev["country"],
            "date": ev["date"],
            "impact": ev["impact"],
            "forecast": ev.get("forecast", ""),
            "previous": ev.get("previous", ""),
        }
    if dates:  # Termine dieser Woche, die nicht mehr im Feed sind und kein Ergebnis haben -> weg
        lo, hi = min(dates), max(dates)
        for k in list(state):
            t = datetime.fromisoformat(state[k]["date"])
            if lo <= t <= hi and k not in feed_keys and not state[k].get("actual"):
                del state[k]


def fetch_actuals(day):
    """Liest die Ergebnisse eines Tages von forexfactory.com. Gibt {(Währung, Titel): (Wert, Tendenz)} zurück."""
    try:
        from bs4 import BeautifulSoup
        from curl_cffi import requests as creq
    except ImportError:
        return {}
    url = f"https://www.forexfactory.com/calendar?day={day.strftime('%b').lower()}{day.day}.{day.year}"
    try:
        r = creq.get(url, impersonate="chrome", timeout=30)
        if r.status_code != 200:
            print(f"  Ergebnisse {day}: HTTP {r.status_code}")
            return {}
    except Exception as e:
        print(f"  Ergebnisse {day}: {e}")
        return {}

    soup = BeautifulSoup(r.text, "html.parser")
    out = {}
    for row in soup.select("tr.calendar__row"):
        cur = row.select_one("td.calendar__currency")
        title = row.select_one(".calendar__event-title")
        act = row.select_one("td.calendar__actual")
        if not (cur and title and act):
            continue
        val = act.get_text(strip=True)
        if not val:
            continue
        tone = ""
        span = act.find("span")
        if span:
            cls = span.get("class") or []
            tone = "better" if "better" in cls else "worse" if "worse" in cls else ""
        out[(cur.get_text(strip=True), title.get_text(strip=True))] = (val, tone)
    return out


def fill_actuals(state):
    now = datetime.now(timezone.utc)
    pending = {}
    for ev in state.values():
        t = datetime.fromisoformat(ev["date"])
        if ev.get("actual") or t > now or now - t > timedelta(days=3):
            continue
        pending.setdefault(t.date(), []).append(ev)  # Datum in FF-Zeitzone (US Eastern)
    for day, evs in pending.items():
        acts = fetch_actuals(day)
        for ev in evs:
            hit = acts.get((ev["country"], ev["title"]))
            if hit:
                ev["actual"], ev["tone"] = hit
                print(f"  ✓ {ev['country']} {ev['title']}: {hit[0]}")


def prune(state):
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    for k in list(state):
        if datetime.fromisoformat(state[k]["date"]) < cutoff:
            del state[k]


def esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    if len(line.encode()) <= 75:
        return line
    parts, cur = [], ""
    for ch in line:
        if len((cur + ch).encode()) > 74:
            parts.append(cur)
            cur = " " + ch
        else:
            cur += ch
    parts.append(cur)
    return "\r\n".join(parts)


def build_ics(state):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    L = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ff-kalender//DE",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "X-WR-CALNAME:ForexFactory", "X-WR-TIMEZONE:Europe/Berlin",
        "REFRESH-INTERVAL;VALUE=DURATION:PT30M", "X-PUBLISHED-TTL:PT30M",
    ]
    for k, ev in sorted(state.items(), key=lambda kv: kv[1]["date"]):
        start = datetime.fromisoformat(ev["date"]).astimezone(timezone.utc)
        end = start + timedelta(minutes=EVENT_MINUTES)

        summary = f"{ICON.get(ev['impact'], '')} {ev['country']} – {ev['title']}"
        if ev.get("actual"):
            summary += f" | {TONE.get(ev.get('tone', ''), '⚪')} {ev['actual']}"

        desc = [
            f"Impact: {ev['impact']}",
            f"Prognose: {ev.get('forecast') or '–'}",
            f"Vorwert: {ev.get('previous') or '–'}",
            f"Ergebnis: {ev.get('actual') or 'noch offen'}",
        ]
        if ev.get("tone") == "better":
            desc.append("→ besser als erwartet (für die Währung)")
        elif ev.get("tone") == "worse":
            desc.append("→ schlechter als erwartet (für die Währung)")

        L += [
            "BEGIN:VEVENT", f"UID:{k}", f"DTSTAMP:{stamp}", f"LAST-MODIFIED:{stamp}",
            f"DTSTART:{start:%Y%m%dT%H%M%SZ}", f"DTEND:{end:%Y%m%dT%H%M%SZ}",
            f"SUMMARY:{esc(summary)}", f"DESCRIPTION:{esc(chr(10).join(desc))}",
            "URL:https://www.forexfactory.com/calendar",
        ]
        if ALARM_MINUTES:
            L += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc(summary)}",
                  f"TRIGGER:-PT{ALARM_MINUTES}M", "END:VALARM"]
        L.append("END:VEVENT")
    L.append("END:VCALENDAR")
    return "\r\n".join(fold(x) for x in L) + "\r\n"


def main():
    state = load_state()
    try:
        merge(state, fetch_feed())
    except Exception as e:
        print(f"Feed nicht erreichbar ({e}) – nutze gespeicherte Daten")
    fill_actuals(state)
    prune(state)

    STATE.parent.mkdir(parents=True, exist_ok=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    OUT.write_text(build_ics(state), encoding="utf-8")
    print(f"{len(state)} Termine -> {OUT}")


if __name__ == "__main__":
    main()
