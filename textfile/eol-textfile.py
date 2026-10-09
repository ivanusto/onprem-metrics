#!/usr/bin/env python3
"""Turn eol/assets.tsv (what has a support end, a licence expiry or a
hardware end-of-support, and where that date comes from) into textfile
metrics, so "how long until X is unsupported" is a number on the dashboard
and an alert 90 days out, not a line in someone's memory.

    eol-textfile.py --assets eol/assets.tsv --out /var/lib/node_exporter/textfile/eol.prom
    eol-textfile.py ... --refresh --cache /var/lib/onprem-metrics/eol      # check dates against endoflife.date
    eol-textfile.py ... --fortigate https://192.168.2.99:10443 --token-file /etc/onprem-metrics/fgt.token
    eol-textfile.py ... --offline       # tests: use the cache, never fetch

Metrics (labels asset, component, kind, version, milestone, basis)
    asset_eol_timestamp_seconds        unix time of the date in the TSV; the rules
                                       turn it into days left and alert at 90/30/0
    asset_eol_unknown                  1 for a row whose date is ? (nothing published
                                       or not looked up yet); the row is the reminder
    asset_eol_source_mismatch          1 when endoflife.date (--refresh) has a
                                       different date than the TSV, 0 when they agree
    asset_eol_source_age_seconds{product}  age of the cached endoflife.date answer
    asset_licence_status{asset,component,status}  1 per FortiGate licence (--fortigate)
    eol_textfile_last_run_timestamp

--refresh reads https://endoflife.date/api/v1/products/<product> for every
distinct product in eol_ref, caches the JSON in --cache (one fetch a day is
plenty; the API asks for that), and compares the release's eolFrom /
eoasFrom / eoesFrom with the TSV according to the milestone. It never
rewrites the TSV: a human changes the date and the note, so the change is
a diff with a reason (Day 26).

--fortigate asks the device for /api/v2/monitor/license/status (read-only
REST token, Day 22's pattern) and emits one asset_eol_timestamp_seconds per
licence that carries an `expires` epoch, plus asset_licence_status. The
response shape is walked generically (any object with a numeric `expires`
is a licence; its key path is the component), so a new licence type shows
up by itself.

Standard library only.
"""
import argparse
import csv
import json
import os
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone

# milestone in the TSV -> field in endoflife.date's v1 release object.
# endoflife.date's three fields are active support (eoas), end of life (eol)
# and extended support (eoes). For Debian that is regular security support,
# LTS and ELTS: 12 has eoasFrom 2026-07-11, eolFrom 2028-06-30, eoesFrom
# 2033-06-30 (read 2026-10-09). Ubuntu LTS has eoas = eol = standard support
# end, and FortiOS has eoas = End of Engineering Support, eol = End of Support.
EOL_FIELD = {
    "end_of_support": "eolFrom",
    "security_support_end": "eoasFrom",
    "end_of_engineering": "eoasFrom",
    "active_support_end": "eoasFrom",
    "lts_end": "eolFrom",
    "extended_support_end": "eoesFrom",
}
LABELS = ("asset", "component", "kind", "version", "milestone", "basis")


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def labels(d, extra=None):
    items = [(k, d.get(k, "")) for k in LABELS]
    if extra:
        items += list(extra.items())
    return "{" + ",".join(f'{k}="{esc(v)}"' for k, v in items) + "}"


def date_ts(s):
    """YYYY-MM-DD -> unix time at 00:00 UTC, or None for ? / empty / bad."""
    if not s or s.strip() in ("?", "-"):
        return None
    try:
        return int(datetime.strptime(s.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def read_assets(path):
    rows = []
    with open(path, encoding="utf-8", newline="") as f:
        lines = [l for l in f if l.strip() and not l.lstrip().startswith("#")]
    rd = csv.DictReader(lines, delimiter="\t")
    need = {"asset", "component", "kind", "version", "milestone", "date", "basis", "eol_ref", "source", "note"}
    missing = need - set(rd.fieldnames or [])
    if missing:
        sys.exit(f"{path}: header lacks {', '.join(sorted(missing))}")
    for i, r in enumerate(rd, 2):
        r = {k: (v or "").strip() for k, v in r.items()}
        if r["basis"] not in ("vendor", "estimate", "licence", "unknown"):
            sys.exit(f"{path}: row {i}: basis must be vendor, estimate, licence or unknown")
        if r["date"] not in ("?", "") and date_ts(r["date"]) is None:
            sys.exit(f"{path}: row {i}: date {r['date']!r} is not YYYY-MM-DD or ?")
        rows.append(r)
    return rows


# ---------- endoflife.date ----------

def fetch_product(product, cache, offline, max_age):
    """Return (release list, cache age seconds) for one product."""
    path = os.path.join(cache, f"{product}.json")
    age = None
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
    if not offline and (age is None or age > max_age):
        req = urllib.request.Request(f"https://endoflife.date/api/v1/products/{product}",
                                     headers={"Accept": "application/json", "User-Agent": "onprem-metrics eol-textfile"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
            os.makedirs(cache, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, path)
            age = 0
        except Exception as e:  # keep the stale cache, say so
            print(f"eol-textfile: endoflife.date {product}: {e}", file=sys.stderr)
    if not os.path.exists(path):
        return None, None
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    rel = doc.get("result", doc).get("releases", [])
    return rel, age


def compare(rows, cache, offline, max_age):
    """-> (mismatch dict keyed by row index: 0/1, {product: age})"""
    products = sorted({r["eol_ref"].split("/", 1)[0] for r in rows if r["eol_ref"] not in ("", "-")})
    data, ages = {}, {}
    for p in products:
        rel, age = fetch_product(p, cache, offline, max_age)
        if rel is not None:
            data[p] = {str(x.get("name", "")): x for x in rel}
            ages[p] = age
    out = {}
    for i, r in enumerate(rows):
        if r["eol_ref"] in ("", "-") or r["date"] == "?":
            continue
        p, _, name = r["eol_ref"].partition("/")
        field = EOL_FIELD.get(r["milestone"])
        rel = data.get(p, {}).get(name)
        if rel is None or field is None:
            continue
        upstream = rel.get(field)
        if upstream is None:
            continue
        out[i] = 0 if upstream == r["date"] else 1
        if out[i]:
            print(f"eol-textfile: {r['asset']} {r['component']} {r['milestone']}: TSV {r['date']}, endoflife.date {upstream} ({p}/{name}.{field})")
    return out, ages


# ---------- FortiGate licences ----------

def walk_licences(obj, path=(), parent_exp=None):
    """Yield (component, status, expires) for every object with a numeric expires.

    Skipped, as seen on a 60F with FortiOS 7.6.7: a free_license (the
    FortiCloud sandbox tier carries the contract's date but never lapses),
    and an object nested in a licence with the same expires
    (iot_detection.definitions repeats iot_detection)."""
    if isinstance(obj, dict):
        exp = obj.get("expires")
        own = None
        if isinstance(exp, (int, float)) and exp > 0:
            own = int(exp)
            status = str(obj.get("status", ""))
            if status != "free_license" and own != parent_exp:
                yield ".".join(path) or "licence", status, own
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                yield from walk_licences(v, path + (str(k),), own if own is not None else parent_exp)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_licences(v, path, parent_exp)


def fortigate_licences(url, token, insecure):
    import ssl
    ctx = ssl._create_unverified_context() if insecure else None   # the lab's 60F has a self-signed GUI cert
    req = urllib.request.Request(url.rstrip("/") + "/api/v2/monitor/license/status",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        doc = json.load(r)
    return list(walk_licences(doc.get("results", doc)))


# ---------- output ----------

def render(rows, mismatch, ages, licences, fgt_asset, now):
    L = []
    def fam(n, h, t="gauge"):
        L.append(f"# HELP {n} {h}"); L.append(f"# TYPE {n} {t}")
    fam("asset_eol_timestamp_seconds", "Unix time of the support end, licence expiry or end-of-order in eol/assets.tsv")
    for r in rows:
        ts = date_ts(r["date"])
        if ts is not None:
            L.append(f"asset_eol_timestamp_seconds{labels(r)} {ts}")
    for comp, status, exp in licences:
        L.append(f'asset_eol_timestamp_seconds{labels({"asset": fgt_asset, "component": comp, "kind": "licence", "version": "-", "milestone": "expires", "basis": "licence"})} {exp}')
    fam("asset_eol_unknown", "1 for an asset whose end date is not published or not yet looked up (date ? in the TSV)")
    for r in rows:
        if r["date"] == "?" and r["basis"] != "licence":
            L.append(f"asset_eol_unknown{labels(r)} 1")
    fam("asset_eol_source_mismatch", "1 when endoflife.date disagrees with the date in the TSV (--refresh), 0 when it agrees")
    for i, v in sorted(mismatch.items()):
        L.append(f"asset_eol_source_mismatch{labels(rows[i])} {v}")
    fam("asset_eol_source_age_seconds", "Age of the cached endoflife.date answer per product")
    for p, age in sorted(ages.items()):
        if age is not None:
            L.append(f'asset_eol_source_age_seconds{{product="{esc(p)}"}} {int(age)}')
    fam("asset_licence_status", "1 per licence the FortiGate reports, with its status label")
    for comp, status, _ in licences:
        L.append(f'asset_licence_status{{asset="{esc(fgt_asset)}",component="{esc(comp)}",status="{esc(status)}"}} 1')
    fam("eol_textfile_last_run_timestamp", "Unix time of the last eol-textfile run")
    L.append(f"eol_textfile_last_run_timestamp {int(now)}")
    return "\n".join(L) + "\n"


def write_atomic(path, body):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".eol.")
    with os.fdopen(fd, "w") as f:
        f.write(body)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--assets", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "eol", "assets.tsv"))
    ap.add_argument("--out", help="write the textfile here; default stdout")
    ap.add_argument("--refresh", action="store_true", help="compare TSV dates with endoflife.date (cached)")
    ap.add_argument("--cache", default="/var/lib/onprem-metrics/eol", help="cache dir for endoflife.date answers")
    ap.add_argument("--max-age", type=int, default=86400, help="seconds before a cached answer is fetched again")
    ap.add_argument("--offline", action="store_true", help="never fetch; use the cache as is (tests)")
    ap.add_argument("--fortigate", help="base URL of the FortiGate, e.g. https://192.168.2.99:10443 (the admin port)")
    ap.add_argument("--token-file", help="file holding the read-only REST token")
    ap.add_argument("--fortigate-asset", default="fgt-edge", help="asset label for the licences")
    ap.add_argument("--insecure", action="store_true", help="do not verify the FortiGate's TLS certificate")
    ap.add_argument("--licence-file", help="tests: a saved /api/v2/monitor/license/status JSON instead of the device")
    a = ap.parse_args()

    rows = read_assets(a.assets)
    mismatch, ages = ({}, {})
    if a.refresh or a.offline:
        mismatch, ages = compare(rows, a.cache, a.offline, a.max_age)
    licences = []
    if a.licence_file:
        with open(a.licence_file, encoding="utf-8") as f:
            doc = json.load(f)
        licences = list(walk_licences(doc.get("results", doc)))
    elif a.fortigate:
        if not a.token_file:
            sys.exit("--fortigate needs --token-file")
        with open(a.token_file, encoding="utf-8") as f:
            token = f.read().strip()
        try:
            licences = fortigate_licences(a.fortigate, token, a.insecure)
        except Exception as e:
            print(f"eol-textfile: FortiGate licence query failed: {e}", file=sys.stderr)
    body = render(rows, mismatch, ages, licences, a.fortigate_asset, time.time())
    if a.out:
        write_atomic(a.out, body)
    else:
        sys.stdout.write(body)


if __name__ == "__main__":
    main()
