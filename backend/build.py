import re
from datetime import datetime

KIND = {"IN": "进闸", "OUT": "出闸", "RS": "倒箱"}
BOX = re.compile(r"^[A-Z]{4}\d{7}$")


def clean_scan(payload: dict):
    scan = str(payload.get("scan") or "").strip().upper()
    box = str(payload.get("box") or "").strip().upper()
    stamp = str(payload.get("at") or "").strip()
    if scan not in KIND:
        return None, "事件码不在字典"
    if not BOX.match(box):
        return None, "箱号不是四位字母加七位数字"
    try:
        event_at = datetime.fromisoformat(stamp)
    except ValueError:
        return None, "扫描时间无法读取"
    return {
        "container_no": box,
        "event_kind": KIND[scan],
        "event_at": event_at,
    }, None
