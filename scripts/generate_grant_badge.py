#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "docs" / "public" / "images" / "ethereum-security-qf-grant.png"

BADGE_SVG = """\
<svg width="512" height="512" viewBox="0 0 512 512" fill="none" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="bg" x1="64" y1="34" x2="462" y2="493" gradientUnits="userSpaceOnUse">
      <stop stop-color="#121B4B"/>
      <stop offset="0.48" stop-color="#101827"/>
      <stop offset="1" stop-color="#0F766E"/>
    </linearGradient>
    <linearGradient id="grant" x1="122" y1="333" x2="390" y2="333" gradientUnits="userSpaceOnUse">
      <stop stop-color="#FF3C38"/>
      <stop offset="1" stop-color="#2DD4BF"/>
    </linearGradient>
    <filter id="shadow" x="-40%" y="-40%" width="180%" height="180%" color-interpolation-filters="sRGB">
      <feDropShadow dx="0" dy="14" stdDeviation="18" flood-color="#000000" flood-opacity="0.26"/>
    </filter>
  </defs>
  <rect width="512" height="512" rx="76" fill="url(#bg)"/>
  <rect x="18" y="18" width="476" height="476" rx="62" stroke="#FFFFFF" stroke-opacity="0.12" stroke-width="2"/>
  <path d="M42 150H470M42 255H470M42 360H470M150 42V470M255 42V470M360 42V470"
        stroke="#FFFFFF" stroke-opacity="0.055" stroke-width="2"/>

  <g filter="url(#shadow)">
    <rect x="76" y="70" width="138" height="138" rx="30" fill="#F8FAFC"/>
    <g transform="translate(101 95) scale(1.76)">
      <path d="M49.6112 25.6113C49.5627 26.307 49.5457 27.0071 49.4608 27.6976C48.5605 35.0305 45.1505 40.9064 39.1644 45.2091C34.6928 48.4241 29.6459 49.9245 24.1488 49.7336C17.7921 49.5144 12.3118 47.1415 7.71639 42.7294C7.6741 42.6887 7.67513 42.5263 7.71639 42.4828C13.448 36.725 19.185 30.973 24.9274 25.2269C25.0632 25.1022 25.2388 25.0301 25.4228 25.0237C33.4863 25.0137 41.5496 25.0108 49.6129 25.0151L49.6112 25.6113Z" fill="#121B4B"/>
      <path d="M25.5293 0.25C26.2072 0.301645 26.8865 0.331743 27.562 0.404935C31.5177 0.834969 35.1608 2.14468 38.4913 4.33406C38.5139 4.35109 38.5355 4.36936 38.5561 4.38879L36.4507 6.49598C35.9924 6.95462 35.5372 7.41635 35.0734 7.86919C35.0441 7.88821 35.0106 7.89974 34.9758 7.90278C34.9411 7.90582 34.9061 7.90028 34.8739 7.88663C31.5209 5.95194 27.9117 5.06211 24.0463 5.21716C15.398 5.56671 7.85866 11.7097 5.785 20.1334C4.50145 25.3526 5.21552 30.3109 7.85866 34.9907C7.8839 35.0352 7.88526 35.1333 7.85491 35.1617C6.70754 36.3209 5.55618 37.4772 4.40085 38.6305C4.3896 38.6418 4.37562 38.651 4.34936 38.6722C4.24058 38.5033 4.13111 38.3398 4.02813 38.1729C1.17765 33.5515 -0.0806642 28.5286 0.326498 23.1069C1.17526 11.8363 9.53948 2.60276 20.6693 0.641955C21.7946 0.443926 22.9455 0.393649 24.0841 0.274967L24.3167 0.25H25.5293Z" fill="#121B4B"/>
      <path d="M38.0391 15.1913C38.041 14.1621 38.3986 13.1653 39.0508 12.3706C39.7031 11.576 40.6098 11.0326 41.6165 10.833C42.6232 10.6334 43.6679 10.7898 44.5725 11.2758C45.4772 11.7617 46.186 12.547 46.5783 13.4981C46.9707 14.4492 47.0223 15.5073 46.7243 16.4922C46.4264 17.4772 45.7973 18.3281 44.9442 18.9002C44.0912 19.4724 43.0668 19.7303 42.0454 19.6301C41.024 19.53 40.0688 19.0779 39.3424 18.3509C38.9287 17.9361 38.6006 17.4435 38.377 16.9013C38.1533 16.3591 38.0385 15.7781 38.0391 15.1913Z" fill="#121B4B"/>
    </g>
  </g>

  <g filter="url(#shadow)">
    <rect x="298" y="70" width="138" height="138" rx="30" fill="#FF3C38"/>
    <path d="M335 170V106H359C368 106 375 107 380 111C386 116 391 123 394 132C397 140 398 149 398 158C398 168 396 177 392 184C388 190 382 195 375 198C370 200 363 201 354 201H335ZM353 184H357C364 184 369 183 373 181C376 179 379 176 381 172C383 168 384 163 384 157C384 146 381 138 376 134C372 130 366 128 356 128H353V184Z" fill="#FFFFFF"/>
    <rect x="323" y="148" width="55" height="12" fill="#FFFFFF"/>
  </g>

  <g transform="translate(205 164)">
    <path d="M51 0L0 85L51 60L102 85L51 0Z" fill="#F8FAFC" fill-opacity="0.92"/>
    <path d="M0 96L51 168L102 96L51 126L0 96Z" fill="#CBD5E1" fill-opacity="0.92"/>
    <path d="M51 60V0L102 85L51 60Z" fill="#94A3B8" fill-opacity="0.82"/>
    <path d="M51 126V168L102 96L51 126Z" fill="#64748B" fill-opacity="0.82"/>
  </g>

  <rect x="60" y="318" width="392" height="102" rx="30" fill="url(#grant)" fill-opacity="0.94"/>
  <text x="256" y="359" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="31" font-weight="800" fill="#F8FAFC">Ethereum Security</text>
  <text x="256" y="397" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="34" font-weight="800" fill="#F8FAFC">QF Grant</text>
  <text x="256" y="456" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="23" font-weight="700" fill="#D8FFF8">Giveth x TheDAO</text>
</svg>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the Ethereum Security QF grant PNG badge.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as tmp:
        tmp.write(BADGE_SVG)
        tmp_path = Path(tmp.name)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(tmp_path),
                "-frames:v",
                "1",
                str(args.out),
            ],
            check=True,
        )
    finally:
        tmp_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
