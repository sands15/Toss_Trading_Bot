"""CBOE 백엔드로 교체한 FMPClient (원본: tradermonty/claude-trading-skills, MIT).

이 파일만 원본과 다르다. 데이터 출처 차이는 ../cboe_fmp_client.py 의 설명을 볼 것.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from cboe_fmp_client import CboeFMPClient as FMPClient  # noqa: E402,F401
