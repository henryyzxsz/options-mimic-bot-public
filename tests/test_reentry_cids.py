from datetime import datetime
from zoneinfo import ZoneInfo
from mimic_bot.paper_loop import leg_cid

NY = ZoneInfo("America/New_York")


def test_reentry_same_contract_gets_unique_stop_and_exit_ids():
    occ = "QQQ261102C00600000"
    a = dict(occ=occ, client_order_id="omb-261102-QQQ-1225-b")
    b = dict(occ=occ, client_order_id="omb-261102-QQQ-1330-b")
    now = datetime(2026, 11, 2, 13, 45, tzinfo=NY)
    assert leg_cid(a, "s", now) != leg_cid(b, "s", now)
    assert leg_cid(a, "x1", now) != leg_cid(b, "x1", now)
    assert leg_cid(b, "s", now) == "omb-261102-QQQ-1330-s"
    assert leg_cid(b, "x1", now) == "omb-261102-QQQ-1330-x1"


def test_legacy_record_without_entry_id_falls_back_to_entry_time():
    t = datetime(2026, 11, 2, 12, 25, tzinfo=NY)
    rec = dict(occ="SPY261102C00601000", time=t)
    assert leg_cid(rec, "s", t).startswith("omb-2611021225-SPY261102C00601000-s")
