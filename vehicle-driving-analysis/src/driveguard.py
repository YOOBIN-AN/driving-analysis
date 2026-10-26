"""
DriveGuard — 블랙박스 영상 기반 차선 상태 · 주행 상태 분석

ACE Lab 하계 코딩캠프(2026.08) 3주차 해커톤 결과물 중
'② 차량 주행 분석' 파트의 분석 파이프라인을 정리한 코드입니다.

notebooks/lane_state_analysis_final.ipynb 의 영상 분석 셀(19~22)을
노트북 없이 실행할 수 있도록 하나의 스크립트로 정리한 버전입니다.
분석 로직과 임계값은 노트북과 동일하며, 노트북 대비 변경점은 아래 두 가지입니다.
  - 전역 변수 대신 RuntimeState 객체로 이동평균·이전 프레임 상태 관리
  - 한쪽 차선만 검출된 프레임에서 발생하던 UnboundLocalError(left_x/right_x) 수정

처리 흐름 (프레임 단위)
    원본 프레임
      → 도로 ROI(사다리꼴) 마스크
      → HLS 흰색·노란색 차선 후보 + Canny 에지
      → HoughLinesP 선분 검출 → 좌/우 차선 분류 → 가중 직선 피팅
      → 차선 중심 · 차선 폭 · 좌우 여유 폭 · 중심 오차(offset)
      → 중앙선 기울기로 진행 방향각(Heading)
      → Optical Flow(Lucas–Kanade 특징점 추적)로 상대 움직임 지수
      → 규칙 기반 위험 단계(NORMAL / CAUTION / DANGER) + 운전자 안내 문구
      → 한글 오버레이 영상(MP4) + 프레임별 결과(CSV)
"""

from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------------------
# 설정값
# ---------------------------------------------------------------------------

# 결과 영상에서 하늘·보닛 위쪽을 잘라 도로 영역을 크게 보여주기 위한 비율
DISPLAY_CROP_TOP_RATIO = 0.22

# 이동평균 창 크기 (프레임 단위 흔들림 완화)
HISTORY_LEN = 7

# 차선 이탈 판단: 차선 폭 대비 중심 편향 비율
DEPARTURE_WARNING_RATIO = 0.10   # 10% 이상 → 편향 주의
DEPARTURE_DANGER_RATIO = 0.25    # 25% 이상 → 이탈

# 진행 방향 판단 (deg)
HEADING_STRAIGHT_DEG = 3.0
HEADING_SHARP_DEG = 12.0

# 좌우 여유 폭 판단
CLOSE_MARGIN_RATIO = 0.25        # 한쪽 여유가 차선 폭의 25% 미만 → 근접
CENTERED_TOLERANCE = 0.10        # 좌우 여유 차이 10% 이내 → 중앙

# Optical Flow 상대 움직임 지수 임계값 (px/frame)
# 실제 km/h가 아니라 영상 해상도·FPS에 따라 조정하는 실험적 기준
SPEED_THRESHOLDS = (0.7, 2.0, 4.5)  # STOPPED < 0.7 ≤ LOW < 2.0 ≤ MEDIUM < 4.5 ≤ HIGH

# 한글 폰트 후보 (Windows → Linux 순)
FONT_CANDIDATES = [
    "C:/Windows/Fonts/malgun.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


# ---------------------------------------------------------------------------
# 실행 상태 (이동평균 · Optical Flow 이전 프레임)
# ---------------------------------------------------------------------------

class RuntimeState:
    """영상 한 개를 처리하는 동안 유지되는 상태값."""

    def __init__(self) -> None:
        self.offset_history: deque = deque(maxlen=HISTORY_LEN)
        self.heading_history: deque = deque(maxlen=HISTORY_LEN)
        self.speed_history: deque = deque(maxlen=HISTORY_LEN)
        self.previous_gray: np.ndarray | None = None

    def reset(self) -> None:
        self.__init__()


# ---------------------------------------------------------------------------
# 1. 차선 후보 추출
# ---------------------------------------------------------------------------

def make_road_roi_mask(image_shape):
    """블랙박스 원본 영상에서 도로가 위치한 사다리꼴 ROI 마스크를 생성한다."""
    h, w = image_shape[:2]

    polygon = np.array([[
        (int(w * 0.08), int(h * 0.98)),
        (int(w * 0.42), int(h * 0.58)),
        (int(w * 0.58), int(h * 0.58)),
        (int(w * 0.92), int(h * 0.98)),
    ]], dtype=np.int32)

    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(mask, polygon, 255)
    return mask, polygon


def build_lane_candidate_mask(frame):
    """HLS 색상 조건과 Canny 에지를 결합해 차선 후보 마스크를 만든다."""
    hls = cv2.cvtColor(frame, cv2.COLOR_BGR2HLS)

    # 흰색 차선: 밝기가 높고 채도가 지나치게 높지 않은 영역
    white_mask = cv2.inRange(
        hls,
        np.array([0, 165, 0], dtype=np.uint8),
        np.array([180, 255, 150], dtype=np.uint8),
    )

    # 노란색 차선
    yellow_mask = cv2.inRange(
        hls,
        np.array([10, 70, 70], dtype=np.uint8),
        np.array([40, 255, 255], dtype=np.uint8),
    )

    color_mask = cv2.bitwise_or(white_mask, yellow_mask)

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 50, 150)

    # 색상 후보 주변의 에지만 남기기 위해 후보를 약간 확장
    dilated_color = cv2.dilate(color_mask, np.ones((5, 5), np.uint8), iterations=1)
    lane_edges = cv2.bitwise_and(edges, dilated_color)

    roi_mask, polygon = make_road_roi_mask(frame.shape)
    lane_edges = cv2.bitwise_and(lane_edges, roi_mask)

    lane_edges = cv2.morphologyEx(
        lane_edges, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)
    )

    return lane_edges, color_mask, polygon


# ---------------------------------------------------------------------------
# 2. 좌/우 차선 검출
# ---------------------------------------------------------------------------

def _fit_lane_line(segments, y_top, y_bottom, frame_width):
    """검출된 여러 선분을 x = a*y + b 직선 하나로 결합한다 (선분 길이 가중)."""
    if not segments:
        return None

    ys, xs, weights = [], [], []
    for x1, y1, x2, y2 in segments:
        length = float(np.hypot(x2 - x1, y2 - y1))
        ys.extend([y1, y2])
        xs.extend([x1, x2])
        weights.extend([length, length])

    if len(xs) < 4:
        return None

    try:
        a, b = np.polyfit(
            np.asarray(ys, dtype=float),
            np.asarray(xs, dtype=float),
            1,
            w=np.asarray(weights, dtype=float),
        )
    except Exception:
        return None

    x_top = int(np.clip(a * y_top + b, 0, frame_width - 1))
    x_bottom = int(np.clip(a * y_bottom + b, 0, frame_width - 1))

    return {
        "x_top": x_top,
        "y_top": int(y_top),
        "x_bottom": x_bottom,
        "y_bottom": int(y_bottom),
        "fit_a": float(a),
        "fit_b": float(b),
    }


def detect_lane_lines(frame, lane_edges):
    """Hough 선분을 왼쪽·오른쪽 차선으로 분류하고 대표 직선을 반환한다."""
    h, w = frame.shape[:2]
    center_x = w / 2.0
    y_top = int(h * 0.60)
    y_bottom = int(h * 0.97)

    lines = cv2.HoughLinesP(
        lane_edges,
        rho=1,
        theta=np.pi / 180,
        threshold=35,
        minLineLength=max(35, int(w * 0.035)),
        maxLineGap=max(50, int(w * 0.055)),
    )

    left_segments, right_segments = [], []

    if lines is not None and len(lines) > 0:
        # OpenCV 버전에 따라 (N,1,4) 또는 (N,4)로 반환되므로 통일
        for line in np.asarray(lines).reshape(-1, 4):
            x1, y1, x2, y2 = map(int, line)
            dx, dy = x2 - x1, y2 - y1
            if dx == 0:
                continue

            slope = dy / dx
            abs_slope = abs(slope)
            midpoint_x = (x1 + x2) / 2.0

            # 수평선 · 지나치게 가파른 구조물 제거
            if abs_slope < 0.35 or abs_slope > 5.0:
                continue

            if slope < 0 and midpoint_x < center_x * 1.08:
                left_segments.append((x1, y1, x2, y2))
            elif slope > 0 and midpoint_x > center_x * 0.92:
                right_segments.append((x1, y1, x2, y2))

    left_line = _fit_lane_line(left_segments, y_top, y_bottom, w)
    right_line = _fit_lane_line(right_segments, y_top, y_bottom, w)
    return left_line, right_line


# ---------------------------------------------------------------------------
# 3. 차선 상태 판단
# ---------------------------------------------------------------------------

def calculate_lane_offset(center_x, vehicle_center_x, lane_width_px):
    """차선 중심과 차량 중심의 차이를 차선 폭 기준으로 정규화한다.

    offset_px > 0 : 차선 중심이 차량 중심보다 오른쪽 → 차량은 차로 왼쪽에 치우침
    """
    if lane_width_px is None or lane_width_px <= 0:
        return {
            "vehicle_center_x": float(vehicle_center_x),
            "lane_center_x": None,
            "offset_px": None,
            "offset_ratio": None,
        }

    offset_px = float(center_x - vehicle_center_x)
    return {
        "vehicle_center_x": float(vehicle_center_x),
        "lane_center_x": float(center_x),
        "offset_px": offset_px,
        "offset_ratio": offset_px / lane_width_px,
    }


def determine_departure_status(offset_ratio):
    """차선 폭 대비 중심 편향 비율로 이탈 상태를 판단한다."""
    if offset_ratio is None:
        return "LANE_UNCERTAIN"

    abs_offset = abs(offset_ratio)
    if abs_offset < DEPARTURE_WARNING_RATIO:
        return "NORMAL"

    if offset_ratio > 0:  # 차량이 차로 왼쪽에 치우침
        return "LEFT_WARNING" if abs_offset < DEPARTURE_DANGER_RATIO else "LEFT_DEPARTURE"
    return "RIGHT_WARNING" if abs_offset < DEPARTURE_DANGER_RATIO else "RIGHT_DEPARTURE"


def determine_heading_status(heading_angle_deg):
    """중앙선 기울기로 추정한 진행 방향각을 단계로 분류한다."""
    angle = float(heading_angle_deg)
    if abs(angle) < HEADING_STRAIGHT_DEG:
        return "STRAIGHT"
    if angle < 0:
        return "SHARP_LEFT" if abs(angle) >= HEADING_SHARP_DEG else "LEFT"
    return "SHARP_RIGHT" if abs(angle) >= HEADING_SHARP_DEG else "RIGHT"


def determine_position_status(left_margin_px, right_margin_px, lane_width_px):
    """차량 좌우 여유 폭으로 차선 내 위치를 판단한다."""
    left_ratio = left_margin_px / lane_width_px
    right_ratio = right_margin_px / lane_width_px

    if left_margin_px <= 0 or right_margin_px <= 0:
        status = "OUT_OF_LANE"
    elif left_ratio < CLOSE_MARGIN_RATIO:
        status = "TOO_CLOSE_LEFT"
    elif right_ratio < CLOSE_MARGIN_RATIO:
        status = "TOO_CLOSE_RIGHT"
    elif abs(left_ratio - right_ratio) <= CENTERED_TOLERANCE:
        status = "CENTERED"
    elif left_ratio < right_ratio:
        status = "LEFT_BIASED"
    else:
        status = "RIGHT_BIASED"

    return status, left_ratio, right_ratio


def _state(risk, status, advice):
    return {"risk_level": risk, "vehicle_status": status, "advice": advice}


def determine_risk_and_advice(departure_status, heading_status, position_status):
    """개별 분석 결과를 우선순위에 따라 통합해 위험 단계와 안내 문구를 만든다.

    우선순위: 차선 인식 실패 → 차선 이탈(DANGER) → 편향·근접(CAUTION)
             → 급곡선(CAUTION) → 정상(NORMAL)
    """
    # 1. 차선 인식 실패 — 실제 위험이 아니라 판단 불가이므로 CAUTION
    if position_status == "LANE_UNCERTAIN" or departure_status == "LANE_UNCERTAIN":
        return _state("CAUTION", "LANE_DETECTION_FAILED",
                      "차선 인식이 불안정합니다. 분석 결과를 주의해서 확인하세요.")

    # 2. 차선 이탈
    if departure_status == "LEFT_DEPARTURE":
        return _state("DANGER", "LEFT_DEPARTURE",
                      "왼쪽 차선을 이탈했습니다. 오른쪽으로 안전하게 복귀하세요.")
    if departure_status == "RIGHT_DEPARTURE":
        return _state("DANGER", "RIGHT_DEPARTURE",
                      "오른쪽 차선을 이탈했습니다. 왼쪽으로 안전하게 복귀하세요.")
    if position_status == "OUT_OF_LANE":
        return _state("DANGER", "OUT_OF_LANE",
                      "차량이 차선 범위를 벗어났습니다. 즉시 차선 안으로 복귀하세요.")

    # 3. 편향 · 근접
    caution_rules = {
        ("departure", "LEFT_WARNING"): "차량이 왼쪽으로 치우쳤습니다. 오른쪽으로 완만하게 이동하세요.",
        ("departure", "RIGHT_WARNING"): "차량이 오른쪽으로 치우쳤습니다. 왼쪽으로 완만하게 이동하세요.",
        ("position", "TOO_CLOSE_LEFT"): "왼쪽 차선에 가깝습니다. 오른쪽으로 완만하게 이동하세요.",
        ("position", "TOO_CLOSE_RIGHT"): "오른쪽 차선에 가깝습니다. 왼쪽으로 완만하게 이동하세요.",
        ("position", "LEFT_BIASED"): "차량이 차선 왼쪽에 치우쳐 있습니다. 차선 중앙으로 이동하세요.",
        ("position", "RIGHT_BIASED"): "차량이 차선 오른쪽에 치우쳐 있습니다. 차선 중앙으로 이동하세요.",
    }
    for (kind, status), advice in caution_rules.items():
        value = departure_status if kind == "departure" else position_status
        if value == status:
            return _state("CAUTION", status, advice)

    # 4. 급격한 도로 방향 변화
    if heading_status == "SHARP_LEFT":
        return _state("CAUTION", "SHARP_LEFT_CURVE",
                      "급한 왼쪽 곡선 구간입니다. 속도를 줄이고 방향을 유지하세요.")
    if heading_status == "SHARP_RIGHT":
        return _state("CAUTION", "SHARP_RIGHT_CURVE",
                      "급한 오른쪽 곡선 구간입니다. 속도를 줄이고 방향을 유지하세요.")

    # 5. 정상
    return _state("NORMAL", "CENTERED", "정상적인 차선 유지 상태입니다.")


# ---------------------------------------------------------------------------
# 4. Optical Flow 기반 상대 움직임
# ---------------------------------------------------------------------------

def estimate_relative_speed(frame, state: RuntimeState):
    """프레임 간 Optical Flow로 영상 기반 상대 속도를 추정한다.

    Returns: {"motion_index", "speed_status", "speed_tracked_points"}
      motion_index : 추적된 특징점의 중앙 이동량(px/frame), 최근 7프레임 평균
      speed_status : STOPPED / LOW / MEDIUM / HIGH / UNAVAILABLE

    이 값은 실제 km/h가 아니라 카메라 영상의 상대적인 움직임 수준이다.
    """
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    def unavailable(points=0):
        return {"motion_index": None, "speed_status": "UNAVAILABLE",
                "speed_tracked_points": int(points)}

    # 하늘, 건물, 앞차 영향을 줄이고 도로 하단 중심부만 사용
    roi_mask = np.zeros_like(gray)
    polygon = np.array([[
        (int(w * 0.12), int(h * 0.96)),
        (int(w * 0.38), int(h * 0.58)),
        (int(w * 0.62), int(h * 0.58)),
        (int(w * 0.88), int(h * 0.96)),
    ]], dtype=np.int32)
    cv2.fillPoly(roi_mask, polygon, 255)

    previous = state.previous_gray
    state.previous_gray = gray
    if previous is None or previous.shape != gray.shape:
        return unavailable()

    previous_points = cv2.goodFeaturesToTrack(
        previous, mask=roi_mask, maxCorners=180,
        qualityLevel=0.01, minDistance=12, blockSize=7,
    )
    if previous_points is None or len(previous_points) < 8:
        return unavailable()

    current_points, status, _ = cv2.calcOpticalFlowPyrLK(
        previous, gray, previous_points, None,
        winSize=(21, 21), maxLevel=3,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
    )
    if current_points is None or status is None:
        return unavailable()

    valid = status.reshape(-1) == 1
    old_points = previous_points.reshape(-1, 2)[valid]
    new_points = current_points.reshape(-1, 2)[valid]
    if len(old_points) < 8:
        return unavailable(len(old_points))

    displacement = np.linalg.norm(new_points - old_points, axis=1)

    # 차량, 반사, 흔들림 등 이상치 영향 완화
    displacement = displacement[np.isfinite(displacement) & (displacement < 60)]
    if len(displacement) < 8:
        return unavailable(len(displacement))

    state.speed_history.append(float(np.median(displacement)))
    motion_index = float(np.mean(state.speed_history))

    # 영상별 해상도와 FPS에 따라 조정 가능한 상대 기준
    stopped, low, medium = SPEED_THRESHOLDS
    if motion_index < stopped:
        speed_status = "STOPPED"
    elif motion_index < low:
        speed_status = "LOW"
    elif motion_index < medium:
        speed_status = "MEDIUM"
    else:
        speed_status = "HIGH"

    return {"motion_index": round(motion_index, 2), "speed_status": speed_status,
            "speed_tracked_points": int(len(displacement))}


# ---------------------------------------------------------------------------
# 5. 프레임 분석
# ---------------------------------------------------------------------------

def analyze_frame(frame, state: RuntimeState):
    """원본 블랙박스 프레임 한 장을 분석해 (오버레이 영상, 결과 dict)를 반환한다."""
    if frame is None or frame.size == 0:
        raise ValueError("비어 있는 프레임입니다.")

    h, w = frame.shape[:2]
    vehicle_center_x = w / 2.0  # 블랙박스가 차량 중앙에 설치되었다고 가정

    lane_edges, _, roi_polygon = build_lane_candidate_mask(frame)
    left_line, right_line = detect_lane_lines(frame, lane_edges)

    reference_y = int(h * 0.90)
    left_x = right_x = None
    lane_center_x = lane_width_px = None
    left_margin_px = right_margin_px = None
    left_margin_ratio = right_margin_ratio = None
    position_status = "LANE_UNCERTAIN"
    raw_heading_angle = 0.0
    offset = calculate_lane_offset(None, vehicle_center_x, None)

    if left_line is not None and right_line is not None:
        left_x = float(np.clip(left_line["fit_a"] * reference_y + left_line["fit_b"], 0, w - 1))
        right_x = float(np.clip(right_line["fit_a"] * reference_y + right_line["fit_b"], 0, w - 1))
        width = right_x - left_x

        # 비현실적으로 좁거나 넓은 쌍은 잘못 매칭된 것으로 처리
        if (w * 0.18) <= width <= (w * 0.75):
            lane_width_px = width
            lane_center_x = (left_x + right_x) / 2.0
            left_margin_px = vehicle_center_x - left_x
            right_margin_px = right_x - vehicle_center_x
            position_status, left_margin_ratio, right_margin_ratio = determine_position_status(
                left_margin_px, right_margin_px, lane_width_px
            )
            offset = calculate_lane_offset(lane_center_x, vehicle_center_x, lane_width_px)

            # 중앙선의 위·아래 위치 차이로 도로 진행 방향 추정
            top_center = (left_line["x_top"] + right_line["x_top"]) / 2.0
            bottom_center = (left_line["x_bottom"] + right_line["x_bottom"]) / 2.0
            dy = max(1.0, left_line["y_bottom"] - left_line["y_top"])
            raw_heading_angle = float(np.degrees(np.arctan2(top_center - bottom_center, dy)))
        else:
            left_line = right_line = None
            left_x = right_x = None

    # 최근 프레임 이동평균으로 상태 깜빡임 완화
    if offset["offset_ratio"] is not None:
        state.offset_history.append(offset["offset_ratio"])
        smoothed = float(np.mean(state.offset_history))
        offset["offset_ratio"] = smoothed
        offset["offset_px"] = smoothed * lane_width_px

    state.heading_history.append(raw_heading_angle)
    heading_angle = float(np.mean(state.heading_history))

    departure_status = determine_departure_status(offset["offset_ratio"])
    heading_status = determine_heading_status(heading_angle)
    final_state = determine_risk_and_advice(departure_status, heading_status, position_status)
    speed = estimate_relative_speed(frame, state)

    def r(value, digits=1):
        return round(value, digits) if value is not None else None

    result = {
        "vehicle_center_x": r(vehicle_center_x),
        "lane_center_x": r(lane_center_x),
        "offset_px": r(offset["offset_px"]),
        "offset_ratio": r(offset["offset_ratio"], 4),
        "departure_status": departure_status,
        "heading_angle_deg": r(heading_angle, 2),
        "heading_status": heading_status,
        **speed,
        "left_lane_x": r(left_x),
        "right_lane_x": r(right_x),
        "lane_width_px": r(lane_width_px),
        "left_margin_px": r(left_margin_px),
        "right_margin_px": r(right_margin_px),
        "left_margin_ratio": r(left_margin_ratio, 4),
        "right_margin_ratio": r(right_margin_ratio, 4),
        "position_status": position_status,
        **final_state,
        "left_line": left_line,
        "right_line": right_line,
        "roi_polygon": roi_polygon[0].tolist(),
    }

    return draw_video_overlay(frame, result), result


# ---------------------------------------------------------------------------
# 6. 시각화
# ---------------------------------------------------------------------------

_FONT_CACHE: dict = {}


def _load_font(size):
    if size not in _FONT_CACHE:
        for path in FONT_CANDIDATES:
            if Path(path).exists():
                _FONT_CACHE[size] = ImageFont.truetype(path, size)
                break
        else:
            raise FileNotFoundError("한글 폰트를 찾을 수 없습니다. FONT_CANDIDATES를 확인하세요.")
    return _FONT_CACHE[size]


def draw_korean_texts(image, items):
    """OpenCV(BGR) 이미지에 한글 텍스트 여러 개를 한 번에 그린다.

    items: [(text, (x, y), font_size, (B, G, R)), ...]
    """
    pil_image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil_image)
    for text, position, size, color in items:
        draw.text(position, text, font=_load_font(size), fill=(color[2], color[1], color[0]))
    return cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)


RISK_KO = {"NORMAL": "정상", "CAUTION": "주의", "DANGER": "위험"}
DEPARTURE_KO = {
    "NORMAL": "정상", "LEFT_WARNING": "왼쪽 편향 주의", "RIGHT_WARNING": "오른쪽 편향 주의",
    "LEFT_DEPARTURE": "왼쪽 차선 이탈", "RIGHT_DEPARTURE": "오른쪽 차선 이탈",
    "LANE_UNCERTAIN": "차선 인식 불안정",
}
HEADING_KO = {
    "STRAIGHT": "직진", "LEFT": "왼쪽 진행", "RIGHT": "오른쪽 진행",
    "SHARP_LEFT": "급한 왼쪽 방향", "SHARP_RIGHT": "급한 오른쪽 방향",
}
SPEED_KO = {"STOPPED": "정지", "LOW": "저속", "MEDIUM": "보통", "HIGH": "고속", "UNAVAILABLE": "측정 중"}
STATUS_KO = {
    "CENTERED": "차선 중앙", "LEFT_WARNING": "왼쪽 편향", "RIGHT_WARNING": "오른쪽 편향",
    "TOO_CLOSE_LEFT": "왼쪽 차선 근접", "TOO_CLOSE_RIGHT": "오른쪽 차선 근접",
    "LEFT_BIASED": "왼쪽으로 치우침", "RIGHT_BIASED": "오른쪽으로 치우침",
    "LEFT_DEPARTURE": "왼쪽 차선 이탈", "RIGHT_DEPARTURE": "오른쪽 차선 이탈",
    "OUT_OF_LANE": "차선 범위 이탈", "LANE_DETECTION_FAILED": "차선 인식 불안정",
    "SHARP_LEFT_CURVE": "급한 왼쪽 곡선", "SHARP_RIGHT_CURVE": "급한 오른쪽 곡선",
}
RISK_COLORS = {"NORMAL": (0, 180, 0), "CAUTION": (0, 190, 255), "DANGER": (0, 0, 230)}


def draw_video_overlay(image, res):
    """원본 영상에 검출 차선 · 중심선 · 한글 분석 패널 · 안내 문구를 표시한다.

    초록 = 검출된 좌/우 차선, 노랑 = 차선 중심, 파랑 = 차량 중심
    """
    original_h = image.shape[0]
    crop_top = int(original_h * DISPLAY_CROP_TOP_RATIO)
    output = image[crop_top:, :].copy()
    h, w = output.shape[:2]

    risk = res["risk_level"]
    color = RISK_COLORS.get(risk, (255, 255, 255))

    def pt(x, y):
        return int(x), int(y - crop_top)

    # 도로 ROI 경계
    poly = np.array([[pt(x, y) for x, y in res["roi_polygon"]]], dtype=np.int32)
    cv2.polylines(output, poly, True, (120, 120, 120), 2)

    # 검출된 차선
    for key in ("left_line", "right_line"):
        line = res.get(key)
        if line is not None:
            cv2.line(output, pt(line["x_bottom"], line["y_bottom"]),
                     pt(line["x_top"], line["y_top"]), (0, 255, 0), 7, cv2.LINE_AA)

    # 차량 중심(파랑) · 차선 중심(노랑)
    vx = int(res["vehicle_center_x"])
    cv2.line(output, (vx, h - 1), (vx, int(h * 0.58)), (255, 0, 0), 4)
    if res["lane_center_x"] is not None:
        lx = int(res["lane_center_x"])
        cv2.line(output, (lx, h - 1), (lx, int(h * 0.58)), (0, 255, 255), 4)

    # 반투명 분석 패널
    scale = w / 1920
    font = max(14, int(30 * scale))
    gap = int(font * 1.5)
    panel = output.copy()
    cv2.rectangle(panel, (20, 20), (int(w * 0.40), 20 + gap * 9 + 20), (0, 0, 0), -1)
    output = cv2.addWeighted(panel, 0.78, output, 0.22, 0)

    def pct(v):
        return "측정 불가" if v is None else f"{v * 100:.1f}%"

    motion = res.get("motion_index")
    lines = [
        ("위험 단계", RISK_KO.get(risk, risk)),
        ("차량 상태", STATUS_KO.get(res["vehicle_status"], res["vehicle_status"])),
        ("차선 상태", DEPARTURE_KO.get(res["departure_status"], res["departure_status"])),
        ("중심 오차", f"{res['offset_px']:.1f} px" if res["offset_px"] is not None else "측정 불가"),
        ("진행 방향각", f"{res['heading_angle_deg']:.2f}°"),
        ("진행 방향", HEADING_KO.get(res["heading_status"], res["heading_status"])),
        ("상대 속도", SPEED_KO.get(res["speed_status"], res["speed_status"])
         + (f" ({motion:.2f} px/frame)" if motion is not None else "")),
        ("왼쪽 여유", pct(res["left_margin_ratio"])),
        ("오른쪽 여유", pct(res["right_margin_ratio"])),
    ]

    # 차선 중앙 복귀 방향 안내 (offset > 0 → 차선 중심이 오른쪽 → 오른쪽으로 이동)
    offset = res["offset_px"]
    if offset is None:
        correction = "차선 인식 확인"
    elif abs(offset) < 5:
        correction = "차선 중앙 유지"
    elif offset > 0:
        correction = "오른쪽으로 이동"
    else:
        correction = "왼쪽으로 이동"

    bar_h = int(65 * max(scale, 0.5))
    cv2.rectangle(output, (20, h - bar_h - 20), (w - 20, h - 20), color, -1)

    texts = [
        (f"{label} : {value}", (40, 30 + i * gap), font, color if i == 0 else (255, 255, 255))
        for i, (label, value) in enumerate(lines)
    ]
    texts.append((correction, (max(20, w - int(360 * scale)), 30), int(font * 1.25), color))
    texts.append((res["advice"], (45, h - bar_h - 12), int(font * 1.1), (0, 0, 0)))
    return draw_korean_texts(output, texts)


def draw_failure_frame(frame):
    """분석 중 예외가 발생한 프레임 표시."""
    crop_top = int(frame.shape[0] * DISPLAY_CROP_TOP_RATIO)
    out = frame[crop_top:, :].copy()
    cv2.rectangle(out, (10, 10), (out.shape[1] - 10, 65), (0, 0, 255), -1)
    cv2.putText(out, "LANE ANALYSIS FAILED", (25, 48), cv2.FONT_HERSHEY_SIMPLEX,
                0.85, (255, 255, 255), 2, cv2.LINE_AA)
    return out


# ---------------------------------------------------------------------------
# 7. 영상 전체 처리
# ---------------------------------------------------------------------------

CSV_DROP_COLUMNS = ("left_line", "right_line", "roi_polygon")


def process_video(input_path, output_video_path, output_csv_path, log_every=100):
    """블랙박스 영상 전체를 분석해 결과 영상(MP4)과 프레임별 결과(CSV)를 저장한다."""
    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        raise RuntimeError(f"입력 영상을 열지 못했습니다: {input_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 20.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    out_h = height - int(height * DISPLAY_CROP_TOP_RATIO)

    Path(output_video_path).parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(output_video_path), cv2.VideoWriter_fourcc(*"mp4v"),
                             fps, (width, out_h))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError("결과 영상 Writer 생성 실패")

    state = RuntimeState()
    records, ok, fail, index = [], 0, 0, 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        record = {"frame": index, "time_sec": round(index / fps, 3)}
        try:
            out, result = analyze_frame(frame, state)
            record.update({k: v for k, v in result.items() if k not in CSV_DROP_COLUMNS})
            record["analysis_success"] = True
            ok += 1
        except Exception as error:  # 한 프레임 실패로 전체가 멈추지 않도록
            out = draw_failure_frame(frame)
            record.update({"analysis_success": False, "error": str(error)})
            fail += 1

        writer.write(out)
        records.append(record)
        index += 1
        if log_every and index % log_every == 0:
            print(f"{index}/{total} 프레임 처리 완료")

    cap.release()
    writer.release()

    df = pd.DataFrame(records)
    df.to_csv(output_csv_path, index=False, encoding="utf-8-sig")
    print(f"완료 — 성공 {ok} / 실패 {fail} 프레임")
    print(f"결과 영상: {output_video_path}\n결과 CSV : {output_csv_path}")
    return df


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    """CSV 결과에서 위험 단계 · 상대 속도 분포를 요약한다."""
    rows = []
    for col in ("risk_level", "vehicle_status", "speed_status"):
        if col in df:
            counts = df[col].value_counts(normalize=True).mul(100).round(1)
            rows += [{"항목": col, "값": k, "비율(%)": v} for k, v in counts.items()]
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description="DriveGuard 블랙박스 영상 주행 분석")
    parser.add_argument("input", help="입력 블랙박스 영상 (mp4)")
    parser.add_argument("--out-dir", default="outputs", help="결과 저장 폴더")
    args = parser.parse_args()

    stem = Path(args.input).stem
    out_dir = Path(args.out_dir)
    df = process_video(args.input,
                       out_dir / f"{stem}_analyzed.mp4",
                       out_dir / f"{stem}_frame_results.csv")
    print(summarize(df).to_string(index=False))


if __name__ == "__main__":
    main()
