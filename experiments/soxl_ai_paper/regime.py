"""시장 국면 점검: tradermonty 감지기 2개를 CBOE 데이터로 돌려 한 번에 요약한다.

- FTD(팔로스루 데이) 감지: 조정 뒤 '진짜 반등'이 확인됐는지
- 시장 고점 감지: 분산일·주도주·방어 업종 순환·지수 기술적 상태·심리로 고점 위험 점수(0~100)

사용:
  python3 regime.py            # 요약 출력 (한국어)
  python3 regime.py --json     # 요약을 JSON으로

S&P 500·나스닥(QQQ) 전체 시장 지표다. 반도체 판단의 '배경'으로 쓰고, 이것만으로 매매하지 않는다.
원본: https://github.com/tradermonty/claude-trading-skills (MIT) — vendor/tradermonty/ 참고.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor", "tradermonty")


def _run(script: str, extra: list[str], out_dir: str, prefix: str) -> dict | None:
    cmd = [sys.executable, os.path.join(VENDOR, script), "--output-dir", out_dir, *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    files = sorted(glob.glob(os.path.join(out_dir, prefix + "*.json")))
    if proc.returncode != 0 or not files:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        print(f"WARN: {script} 실패: {' / '.join(tail)}", file=sys.stderr)
        return None
    with open(files[-1], encoding="utf-8") as fh:
        return json.load(fh)


def summarize(ftd: dict | None, top: dict | None) -> dict:
    s: dict = {"source": "CBOE delayed quotes + tradermonty detectors"}
    if ftd:
        q = ftd["quality_score"]
        s["ftd"] = {
            "state": ftd["market_state"]["combined_state"],
            "sp500_correction_pct": ftd["sp500"].get("correction_depth_pct"),
            "nasdaq_correction_pct": ftd["nasdaq"].get("correction_depth_pct"),
            "signal": q.get("signal"),
            "quality_score": q.get("total_score"),
            "exposure_range": q.get("exposure_range"),
            "guidance": q.get("guidance"),
        }
    if top:
        c = top["composite"]
        s["market_top"] = {
            "risk_score": c.get("composite_score"),
            "zone": c.get("zone"),
            "risk_budget": c.get("risk_budget"),
            "strongest_warning": c.get("strongest_warning"),
            "components": c.get("component_scores"),
            "note": "breadth_divergence는 데이터 없음(중립 50) — 시장 폭 자동 수집 비활성",
        }
    return s


def to_text(s: dict) -> str:
    lines = ["[시장 국면 점검] (S&P 500·나스닥 전체 시장, CBOE 데이터)"]
    f = s.get("ftd")
    if f:
        lines.append(f"- 국면: {f['state']} (S&P 고점 대비 {f['sp500_correction_pct']}%, "
                     f"나스닥(QQQ) {f['nasdaq_correction_pct']}%)")
        lines.append(f"- 반등 확인(FTD): {f['signal']} / 품질 {f['quality_score']}/100, "
                     f"권장 주식 비중 {f['exposure_range']}")
    else:
        lines.append("- FTD 감지: 실패(데이터 없음)")
    t = s.get("market_top")
    if t:
        sw = t.get("strongest_warning")
        sw_txt = sw.get("label") if isinstance(sw, dict) else sw
        lines.append(f"- 고점 위험 점수: {t['risk_score']}/100, 구간 {t['zone']}, "
                     f"권장 위험 예산 {t['risk_budget']}, 가장 강한 경고: {sw_txt}")
    else:
        lines.append("- 고점 감지: 실패(데이터 없음)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    with tempfile.TemporaryDirectory() as out:
        ftd = _run("ftd-detector/ftd_detector.py", [], out, "ftd_detector_")
        top = _run("market-top-detector/market_top_detector.py", ["--no-auto-breadth"], out, "market_top_")
    s = summarize(ftd, top)
    print(json.dumps(s, ensure_ascii=False, indent=2) if args.json else to_text(s))
    return 0 if (ftd or top) else 1


if __name__ == "__main__":
    raise SystemExit(main())
