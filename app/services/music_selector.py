from app.core.config import cfg


def select_track(jolt_count=0):
    order = cfg.TRACK_ORDER
    t = order[jolt_count % len(order)]
    print(f"[music] jolt #{jolt_count} -> {t}")
    return t