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
    <linearGradient id="bg" x1="35" y1="70" x2="486" y2="426" gradientUnits="userSpaceOnUse">
      <stop stop-color="#6256D6"/>
      <stop offset="0.52" stop-color="#4856B0"/>
      <stop offset="1" stop-color="#285C72"/>
    </linearGradient>
    <linearGradient id="grant" x1="60" y1="352" x2="452" y2="352" gradientUnits="userSpaceOnUse">
      <stop stop-color="#E83430"/>
      <stop offset="0.58" stop-color="#5060B8"/>
      <stop offset="1" stop-color="#285C72"/>
    </linearGradient>
    <filter id="shadow" x="-40%" y="-40%" width="180%" height="180%" color-interpolation-filters="sRGB">
      <feDropShadow dx="0" dy="14" stdDeviation="18" flood-color="#000000" flood-opacity="0.26"/>
    </filter>
  </defs>
  <rect width="512" height="512" rx="76" fill="url(#bg)"/>
  <rect x="18" y="18" width="476" height="476" rx="62" stroke="#FFFFFF" stroke-opacity="0.12" stroke-width="2"/>
  <path d="M448 -18C382 14 350 80 350 154C350 239 405 311 480 334M482 38C420 62 384 111 384 170C384 239 428 298 488 322"
        stroke="#7EB5C3" stroke-opacity="0.28" stroke-width="3"/>
  <rect x="80" y="92" width="80" height="80" rx="2" fill="#8190DF" fill-opacity="0.16"/>
  <rect x="174" y="118" width="82" height="82" rx="2" fill="#8190DF" fill-opacity="0.13"/>
  <rect x="68" y="346" width="82" height="82" rx="2" fill="#8190DF" fill-opacity="0.13"/>
  <rect x="386" y="126" width="84" height="84" rx="2" fill="#5D83B5" fill-opacity="0.14"/>
  <rect x="274" y="390" width="82" height="82" rx="2" fill="#5D83B5" fill-opacity="0.14"/>

  <g filter="url(#shadow)">
    <rect x="76" y="70" width="138" height="138" rx="30" fill="#F8FAFC"/>
    <g transform="translate(101 95) scale(1.76)">
      <path d="M49.6112 25.6113C49.5627 26.307 49.5457 27.0071 49.4608 27.6976C48.5605 35.0305 45.1505 40.9064 39.1644 45.2091C34.6928 48.4241 29.6459 49.9245 24.1488 49.7336C17.7921 49.5144 12.3118 47.1415 7.71639 42.7294C7.6741 42.6887 7.67513 42.5263 7.71639 42.4828C13.448 36.725 19.185 30.973 24.9274 25.2269C25.0632 25.1022 25.2388 25.0301 25.4228 25.0237C33.4863 25.0137 41.5496 25.0108 49.6129 25.0151L49.6112 25.6113Z" fill="#121B4B"/>
      <path d="M25.5293 0.25C26.2072 0.301645 26.8865 0.331743 27.562 0.404935C31.5177 0.834969 35.1608 2.14468 38.4913 4.33406C38.5139 4.35109 38.5355 4.36936 38.5561 4.38879L36.4507 6.49598C35.9924 6.95462 35.5372 7.41635 35.0734 7.86919C35.0441 7.88821 35.0106 7.89974 34.9758 7.90278C34.9411 7.90582 34.9061 7.90028 34.8739 7.88663C31.5209 5.95194 27.9117 5.06211 24.0463 5.21716C15.398 5.56671 7.85866 11.7097 5.785 20.1334C4.50145 25.3526 5.21552 30.3109 7.85866 34.9907C7.8839 35.0352 7.88526 35.1333 7.85491 35.1617C6.70754 36.3209 5.55618 37.4772 4.40085 38.6305C4.3896 38.6418 4.37562 38.651 4.34936 38.6722C4.24058 38.5033 4.13111 38.3398 4.02813 38.1729C1.17765 33.5515 -0.0806642 28.5286 0.326498 23.1069C1.17526 11.8363 9.53948 2.60276 20.6693 0.641955C21.7946 0.443926 22.9455 0.393649 24.0841 0.274967L24.3167 0.25H25.5293Z" fill="#121B4B"/>
      <path d="M38.0391 15.1913C38.041 14.1621 38.3986 13.1653 39.0508 12.3706C39.7031 11.576 40.6098 11.0326 41.6165 10.833C42.6232 10.6334 43.6679 10.7898 44.5725 11.2758C45.4772 11.7617 46.186 12.547 46.5783 13.4981C46.9707 14.4492 47.0223 15.5073 46.7243 16.4922C46.4264 17.4772 45.7973 18.3281 44.9442 18.9002C44.0912 19.4724 43.0668 19.7303 42.0454 19.6301C41.024 19.53 40.0688 19.0779 39.3424 18.3509C38.9287 17.9361 38.6006 17.4435 38.377 16.9013C38.1533 16.3591 38.0385 15.7781 38.0391 15.1913Z" fill="#121B4B"/>
    </g>
  </g>

  <g filter="url(#shadow)">
    <path d="M368 58C389 72 416 78 437 76L439 152C434 194 404 224 368 242C332 224 302 194 297 152L299 76C320 78 347 72 368 58Z" fill="#F8FAFC"/>
    <path d="M368 71C386 83 409 88 426 87L428 149C424 185 398 211 368 226C338 211 312 185 308 149L310 87C327 88 350 83 368 71Z" fill="#E83430"/>
    <path d="M368 86C383 96 401 100 416 99L417 146C414 176 392 198 368 211C344 198 322 176 319 146L320 99C335 100 353 96 368 86Z" fill="none" stroke="#F8FAFC" stroke-width="4"/>
    <path d="M346 174V113H368C377 113 384 115 389 120C394 125 398 133 399 144C401 156 399 166 394 174C389 183 381 187 369 187H346ZM362 173H367C373 173 378 171 381 167C384 162 386 155 386 147C386 139 384 133 380 129C377 126 372 124 366 124H362V173Z" fill="#FFFFFF"/>
    <rect x="337" y="144" width="42" height="10" fill="#FFFFFF"/>
  </g>

  <g transform="translate(205 164)">
    <path d="M51 0L0 85L51 60L102 85L51 0Z" fill="#F8FAFC" fill-opacity="0.92"/>
    <path d="M0 96L51 168L102 96L51 126L0 96Z" fill="#CBD5E1" fill-opacity="0.92"/>
    <path d="M51 60V0L102 85L51 60Z" fill="#94A3B8" fill-opacity="0.82"/>
    <path d="M51 126V168L102 96L51 126Z" fill="#64748B" fill-opacity="0.82"/>
  </g>

  <rect x="60" y="318" width="392" height="102" rx="30" fill="url(#grant)" fill-opacity="0.95"/>
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
