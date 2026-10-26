#!/usr/bin/env python3
"""AP RF stats, 9800 tags, controller resolution, hierarchy-walking device lookup.
Field names pinned against Catalyst Center 2.3.7.10 device-detail output."""
import logging
from collections import Counter

log = logging.getLogger("catc-report")

MAX_APS_PER_SITE = 60
MAX_PARENT_HOPS = 6
AP_FAMILY = "Unified AP"
WLC_FAMILY = "Wireless Controller"

# assuranceEvents caps limit at 20 (errorCode 14007) — paginate instead
EVENT_PAGE_LIMIT = 20
MAX_EVENT_PAGES = 25          # up to 500 events per site

DEBUG = False

# 'utilization' IS channel utilization %. 'utilizationScore' is a 1-10 health
# score — do not confuse them.
RF_KEYS = {
    "channel_util": "utilization",
    "noise": "noise",
    "interference": "interference",
    "air_quality": "airQuality",
}

# 9800 config-model tags, already present in device-detail — zero extra API cost
TAG_KEYS = {
    "site_tag": "siteTagName",
    "rf_tag": "rfTagName",
    "policy_tag": "policyTagName",
}


# ───────────────────────── hierarchy walk ─────────────────────────
def _parent_chain(c, site_id, max_hops=MAX_PARENT_HOPS):
    """Yield site_id, then each ancestor id, walking up toward Global."""
    seen, sid, hops = set(), site_id, 0
    while sid and sid not in seen and hops <= max_hops:
        yield sid
        seen.add(sid)
        hops += 1
        try:
            info = c.get(f"/dna/intent/api/v1/site/{sid}").get("response", {})
            if isinstance(info, list):
                info = info[0] if info else {}
            sid = info.get("parentId")
        except Exception as e:
            log.debug("parent lookup failed for %s: %s", sid, e)
            return


def _devices_at(c, sid, family=None):
    params = {"siteId": sid}
    if family:
        params["family"] = family
    try:
        resp = c.get("/dna/intent/api/v1/network-device", params=params)
        return resp.get("response", []) if isinstance(resp, dict) else []
    except Exception as e:
        log.debug("network-device fetch failed for %s: %s", sid, e)
        return []


def _names(devices):
    out = []
    for m in devices:
        val = m.get("hostname") or m.get("name") or m.get("managementIpAddress")
        if val:
            out.append(str(val))
    return out


def get_site_devices(c, site_id):
    """All device names for a site, walking Floor -> Building -> Area -> ... ."""
    for hop, sid in enumerate(_parent_chain(c, site_id)):
        names = _names(_devices_at(c, sid))
        if names:
            if hop:
                log.debug("Site %s: devices resolved %d level(s) up", site_id, hop)
            return names
    return []


def get_site_aps(c, site_id):
    for sid in _parent_chain(c, site_id):
        aps = _devices_at(c, sid, family=AP_FAMILY)
        if aps:
            return aps
    return []


def get_site_controller(c, site_id):
    """site-health has no controller field on 2.3.7.10 — resolve WLC from
    inventory instead. Returns a display string, never None."""
    for sid in _parent_chain(c, site_id):
        names = _names(_devices_at(c, sid, family=WLC_FAMILY))
        if names:
            return "; ".join(sorted(set(names)))
    return "N/A"


# ───────────────────────── RF + tags ─────────────────────────
def _num_multi(v):
    """Catalyst returns per-radio values as a comma-joined string
    ('46.0,16.0' = slot0,slot1). Returns (worst, [per-radio...])."""
    if v is None:
        return None, []
    if isinstance(v, (int, float)):
        return float(v), [float(v)]
    parts = []
    for chunk in str(v).split(","):
        chunk = chunk.strip().rstrip("%")
        if not chunk:
            continue
        try:
            parts.append(float(chunk))
        except ValueError:
            continue
    if not parts:
        return None, []
    return max(parts), parts


def _ap_rf_detail(c, ap_uuid, ts_ms):
    blank = {k: None for k in RF_KEYS}
    blank.update({k: None for k in TAG_KEYS})
    blank["name"] = None
    blank["radio_util"] = []
    if not ap_uuid:
        return blank
    try:
        resp = c.get("/dna/intent/api/v1/device-detail",
                     params={"timestamp": ts_ms, "searchBy": ap_uuid,
                             "identifier": "uuid"})
    except Exception as e:
        log.debug("device-detail failed for %s: %s", ap_uuid, e)
        return blank

    d = resp.get("response", {}) if isinstance(resp, dict) else {}
    if not d:
        return blank

    out = {}
    for label, key in RF_KEYS.items():
        worst, parts = _num_multi(d.get(key))
        out[label] = worst
        if label == "channel_util":
            out["radio_util"] = parts
    out.update({label: d.get(key) for label, key in TAG_KEYS.items()})
    out["name"] = d.get("nwDeviceName")

    if DEBUG:
        log.debug("AP %s util=%r -> worst=%s radios=%s siteTag=%r rfTag=%r",
                  out["name"], d.get("utilization"), out["channel_util"],
                  out["radio_util"], out.get("site_tag"), out.get("rf_tag"))
    return out

def _channel_changes_by_ap(c, site_id, start, end):
    """Count RRM channel-change events per AP. Paginated at 20/page (API cap)."""
    counts = Counter()
    offset, pages = 1, 0
    while pages < MAX_EVENT_PAGES:
        try:
            resp = c.get("/dna/data/api/v1/assuranceEvents",
                         params={"deviceFamily": AP_FAMILY, "siteId": site_id,
                                 "startTime": start, "endTime": end,
                                 "limit": EVENT_PAGE_LIMIT, "offset": offset})
        except Exception as e:
            log.debug("assuranceEvents page at offset %d failed: %s", offset, e)
            break

        events = resp.get("response", []) if isinstance(resp, dict) else []
        if not events:
            break
        if DEBUG and pages == 0:
            log.info("assuranceEvents keys: %s", list(events[0].keys()))

        for ev in events:
            blob = " ".join(str(ev.get(k, "")) for k in
                            ("name", "eventName", "messageType",
                             "details", "description")).lower()
            if "channel" in blob and "chang" in blob:
                ap = (ev.get("networkDeviceName") or ev.get("deviceName")
                      or ev.get("name"))
                if ap:
                    counts[str(ap)] += 1

        if len(events) < EVENT_PAGE_LIMIT:
            break
        offset += EVENT_PAGE_LIMIT
        pages += 1

    if counts:
        log.debug("Site %s: %d channel-change event(s) across %d AP(s)",
                  site_id, sum(counts.values()), len(counts))
    return counts


def get_ap_rf_stats(c, site_id, start, end):
    """[{name, channel_util, channel_changes, noise, interference,
         air_quality, site_tag, rf_tag, policy_tag}, ...]"""
    aps = get_site_aps(c, site_id)
    if not aps:
        return []

    truncated = len(aps) > MAX_APS_PER_SITE
    if truncated:
        log.warning("Site %s has %d APs — capping at %d (tag list may be partial)",
                    site_id, len(aps), MAX_APS_PER_SITE)
        aps = aps[:MAX_APS_PER_SITE]

    changes = _channel_changes_by_ap(c, site_id, start, end)

    rows = []
    for ap in aps:
        inv_name = (ap.get("hostname") or ap.get("name")
                    or ap.get("managementIpAddress") or "?")
        rf = _ap_rf_detail(c, ap.get("id"), end)
        name = str(rf.get("name") or inv_name)
        rows.append({
            "name": name,
            "channel_util": rf["channel_util"],        # None stays None, not 0.0
            "radio_util": rf.get("radio_util", []),
            "channel_changes": changes.get(name, 0) or changes.get(str(inv_name), 0),
            "noise": rf["noise"],
            "interference": rf["interference"],
            "air_quality": rf["air_quality"],
            "site_tag": rf.get("site_tag"),
            "rf_tag": rf.get("rf_tag"),
            "policy_tag": rf.get("policy_tag"),
            "_truncated": truncated,
        })

    log.info("Site %s: RF stats for %d AP(s)", site_id, len(rows))
    return rows


def summarize_tags(ap_rows):
    """Collapse per-AP tags into distinct per-site values for the site CSV.
    Appends '(partial)' when the AP cap truncated the sample."""
    partial = any(r.get("_truncated") for r in ap_rows)
    out = {}
    for label in TAG_KEYS:
        vals = sorted({str(r.get(label)) for r in ap_rows if r.get(label)})
        if not vals:
            out[label] = "—"
        else:
            joined = "; ".join(vals)
            out[label] = f"{joined} (partial)" if partial else joined
    return out
