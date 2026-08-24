"""Scan real public web pages with the product's own scanner, at fleet scale.

Why this does not just call `app.scanner.scan_page` in a loop:

  * `scan_page` launches and tears down a whole browser per call. At one
    target that is irrelevant; at two hundred it is most of the wall clock.
  * `scan_page` waits for `networkidle`, which is correct for the fixture the
    remediation loop serves locally and wrong for the open web -- analytics
    beacons, chat widgets and long-poll connections mean plenty of real sites
    never go idle, and the call hangs until the default timeout.
  * A fleet run must survive a dead host. One unreachable target should cost
    that row, not the run.

The axe invocation and the node-level accounting are deliberately identical to
`scan_page`, so a number produced here means the same thing as a number
produced by a remediation run.
"""

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.scanner import SUPPORTED_RULES, _axe_source  # noqa: E402

CONCURRENCY = 8
PAGE_TIMEOUT_MS = 35_000
SETTLE_MS = 2_500


async def scan_one(context, target: dict, axe: str) -> dict:
    row = {"repo": target["repo"], "url": target["url"], "stars": target["stars"]}
    page = await context.new_page()
    try:
        await page.goto(target["url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        await page.wait_for_timeout(SETTLE_MS)
        await page.add_script_tag(content=axe)
        raw = await page.evaluate("async () => JSON.stringify(await axe.run())")
        by_rule: dict[str, int] = {}
        for entry in json.loads(raw)["violations"]:
            by_rule[entry["id"]] = by_rule.get(entry["id"], 0) + len(entry["nodes"])
        row |= {
            "ok": True,
            "rules": len(by_rule),
            "nodes": sum(by_rule.values()),
            "patchable": sum(n for r, n in by_rule.items() if r in SUPPORTED_RULES),
            "by_rule": by_rule,
        }
    except Exception as exc:  # a dead host costs its row, not the run
        row |= {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:90]}"}
    finally:
        await page.close()
    return row


async def main(targets_path: str, out_path: str, limit: int | None) -> None:
    targets = json.loads(Path(targets_path).read_text())
    if limit:
        targets = targets[:limit]
    axe = _axe_source()
    out = Path(out_path)
    done = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
        )
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def guarded(t):
            nonlocal done
            async with semaphore:
                row = await scan_one(context, t, axe)
                done += 1
                flag = "ok " if row["ok"] else "ERR"
                print(f"[{done:>3}/{len(targets)}] {flag} {row.get('patchable', 0):>4} patchable "
                      f"/ {row.get('nodes', 0):>4} nodes  {row['repo']}", flush=True)
                # append as we go: a crash at target 180 must not lose 179 scans
                with out.open("a") as fh:
                    fh.write(json.dumps(row) + "\n")
                return row

        await asyncio.gather(*(guarded(t) for t in targets))
        await browser.close()


if __name__ == "__main__":
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else None
    asyncio.run(main(sys.argv[1], sys.argv[2], limit))
