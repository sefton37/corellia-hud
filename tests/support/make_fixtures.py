"""CLI for the e2e run: python3 -m tests.support.make_fixtures <dir> <registry: healthy|mixed|rich|allgreen|detail> [--token] [--hostile]

`detail` also writes the detail-window fixtures: a chat data tree under <dir>, beacon rows with paths and
referrers, daily_countries / daily_interactions rows, and a failing health check. `--hostile` plants markup
payloads in a conversation, a beacon path and referrer."""
import os
import sys
import time

from tests.support import fixtures as fx


def main():
    out, kind = sys.argv[1], sys.argv[2]
    hostile = "--hostile" in sys.argv
    os.makedirs(out, exist_ok=True)
    now = time.time()
    text = {"healthy": fx.REGISTRY_E2E_HEALTHY, "mixed": fx.REGISTRY_E2E_MIXED, "rich": None,
            "allgreen": fx.allgreen_registry(), "detail": fx.REGISTRY_E2E_DETAIL}[kind]
    fx.write(os.path.join(out, "registry.yaml"), text if text is not None else fx.rich_registry())
    events = fx.beacon_events(now) + (fx.detail_events(now, hostile) if kind == "detail" else [])
    adb = fx.make_analytics(os.path.join(out, "analytics.db"), events)
    runs = fx.healthy_product_runs(now) + ([("hc-9", "fail", now - 60)] if kind == "detail" else [])
    fx.make_product(os.path.join(out, "product.db"), runs)
    if kind == "detail":
        yesterday = fx.utc_str(now - 86400)[:10]
        fx.add_rows(adb, "INSERT INTO daily_countries (site,date,country,pageviews) VALUES (?,?,?,?)",
                    [("kellogg", yesterday, "US", 40), ("kellogg", yesterday, "DE", 7)])
        fx.add_rows(adb, "INSERT INTO daily_interactions (site,date,kind,count) VALUES (?,?,?,?)",
                    [("kellogg", yesterday, "chat_open", 4)])
        fx.make_chat_tree(os.path.join(out, "chatdata"), now, hostile=hostile)
    if "--token" in sys.argv:
        path = os.path.join(out, "cloudflare.token")
        fx.write(path, "e2e-fixture-token\n")
        os.chmod(path, 0o600)
    print(out)


if __name__ == "__main__":
    main()
