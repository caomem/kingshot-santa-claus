import argparse
import asyncio
import csv
import os
import sys
from datetime import datetime
from playwright.async_api import async_playwright

SITE_URL = "https://ks-giftcode.centurygame.com/"


def load_players_from_file(file_path: str) -> list:
    """Load player entries from a file supporting 'Name,PlayerID' or 'PlayerID' format."""
    if not os.path.exists(file_path):
        print(f"[!] Error: File not found at '{file_path}'")
        sys.exit(1)

    players = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if "," in line:
                parts = line.split(",", 1)
                name = parts[0].strip()
                player_id = parts[1].strip()
            else:
                name = "N/A"
                player_id = line

            if player_id:
                players.append({"name": name, "id": player_id})

    return players


def load_codes_from_file(file_path: str) -> list:
    """Load gift codes from a text file."""
    if not os.path.exists(file_path):
        print(f"[!] Error: File not found at '{file_path}'")
        sys.exit(1)

    with open(file_path, "r", encoding="utf-8") as f:
        codes = [
            line.strip()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        ]
    return codes


async def redeem_batch(
    players: list,
    codes: list,
    delay: float = 1.5,
    headless: bool = True,
    output_csv: str = "redemption_results.csv",
):
    results = []

    print("\n==================================================")
    print("  KINGSHOT AUTO-REDEEMER CLI (Batch Mode)")
    print(f"  Total Players: {len(players)}")
    print(f"  Total Codes:   {len(codes)}")
    print("==================================================\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        for p_idx, player in enumerate(players, 1):
            player_name = player["name"]
            player_id = player["id"]

            print(
                f"\n[{p_idx}/{len(players)}] 👤 Logging in Player: '{player_name}' (ID: {player_id})..."
            )

            try:
                # 1. Navigate to portal and submit Player ID
                await page.goto(SITE_URL, wait_until="networkidle")
                await page.wait_for_timeout(1000)

                inputs = await page.query_selector_all("input")
                if not inputs:
                    print("   [!] Error: Form input elements not found.")
                    continue

                await inputs[0].fill(player_id)

                login_btn = await page.query_selector(
                    "button:has-text('Login'), button:has-text('Confirm'), .login-btn"
                )
                if login_btn:
                    await login_btn.click()
                else:
                    await page.keyboard.press("Enter")

                await page.wait_for_timeout(2000)

                # 2. Iterate through codes for current player
                for c_idx, code in enumerate(codes, 1):
                    code = code.strip()
                    print(
                        f"   ({c_idx}/{len(codes)}) 🎁 Redeeming '{code}'...",
                        end=" ",
                        flush=True,
                    )

                    inputs_after = await page.query_selector_all("input")
                    code_input = (
                        inputs_after[1]
                        if len(inputs_after) > 1
                        else inputs_after[0]
                    )

                    await code_input.fill("")
                    await code_input.fill(code)

                    confirm_btn = await page.query_selector(
                        "button:has-text('Confirm'), button:has-text('Redeem'), .confirm-btn"
                    )
                    if confirm_btn:
                        await confirm_btn.click()
                    else:
                        await page.keyboard.press("Enter")

                    await page.wait_for_timeout(2000)

                    # 3. Parse result notification modal
                    msg = "Processed"
                    status = "SUCCESS"

                    modal = await page.query_selector(
                        ".el-message, .dialog, .toast, .notice, [role='alert']"
                    )
                    if modal:
                        msg = (
                            (await modal.inner_text())
                            .strip()
                            .replace("\n", " ")
                        )
                        msg_lower = msg.lower()

                        if (
                            "already" in msg_lower
                            or "claimed" in msg_lower
                            or "used" in msg_lower
                        ):
                            status = "ALREADY_CLAIMED"
                        elif (
                            "expired" in msg_lower or "invalid" in msg_lower
                        ):
                            status = "EXPIRED_OR_INVALID"
                        elif (
                            "success" in msg_lower
                            or "congratulations" in msg_lower
                        ):
                            status = "SUCCESS"
                        else:
                            status = "SITE_RESPONSE"

                    print(f"-> [{status}] ({msg})")

                    results.append(
                        {
                            "timestamp": datetime.now().strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                            "player_name": player_name,
                            "player_id": player_id,
                            "code": code,
                            "status": status,
                            "message": msg,
                        }
                    )

                    await asyncio.sleep(delay)

            except Exception as e:
                print(
                    f"   [!] Exception encountered for Player '{player_name}' ({player_id}): {e}"
                )

        await browser.close()

    # 4. Export execution metrics to CSV
    if results and output_csv:
        with open(output_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "timestamp",
                    "player_name",
                    "player_id",
                    "code",
                    "status",
                    "message",
                ],
            )
            writer.writeheader()
            writer.writerows(results)
        print(f"\n[✓] Detailed report saved to: '{output_csv}'")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Kingshot Santa Claus: Gift Code Redeemer"
    )

    player_group = parser.add_mutually_exclusive_group(required=True)
    player_group.add_argument(
        "-u",
        "--userid",
        help="Single Player ID or 'Name,ID' (e.g. 'caomem,360963190')",
    )
    player_group.add_argument(
        "-U",
        "--users-file",
        help="Path to file containing Player IDs or 'Name,ID' per line",
    )

    code_group = parser.add_mutually_exclusive_group(required=True)
    code_group.add_argument(
        "-c", "--code", nargs="+", help="One or multiple codes separated by space"
    )
    code_group.add_argument(
        "-C",
        "--codes-file",
        help="Path to .txt file with Gift Codes (one per line)",
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="Delay between redemption attempts in seconds (default: 1.5s)",
    )
    parser.add_argument(
        "--out",
        default="redemption_results.csv",
        help="Output CSV file path (default: redemption_results.csv)",
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Display browser UI during execution",
    )

    args = parser.parse_args()

    if args.userid:
        if "," in args.userid:
            name, p_id = args.userid.split(",", 1)
            players = [{"name": name.strip(), "id": p_id.strip()}]
        else:
            players = [{"name": "N/A", "id": args.userid.strip()}]
    else:
        players = load_players_from_file(args.users_file)

    codes = args.code if args.code else load_codes_from_file(args.codes_file)

    asyncio.run(
        redeem_batch(
            players=players,
            codes=codes,
            delay=args.delay,
            headless=not args.show_browser,
            output_csv=args.out,
        )
    )