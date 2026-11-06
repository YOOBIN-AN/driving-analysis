[← 차량 주행 분석](../README.md)

# archive

최종 노트북 [`lane_state_analysis_final.ipynb`](../notebooks/lane_state_analysis_final.ipynb)에 이르기까지의 개발 노트북입니다.
v1 ~ v2는 수업 함수로 차선을 검출했고, v3부터 블랙박스 시점에 맞는 방식으로 바꿨습니다.

### 1단계 · 수업 함수 기반 차선 검출

BEV 변환 → `dominant_gradient` → `get_lane_center`

| 버전 | 파일 | 변경 내용 |
|:-:|---|---|
| v1 | [`lane_state_analysis_v1.ipynb`](lane_state_analysis_v1.ipynb) | 수업 파이프라인을 블랙박스 영상 프레임에 그대로 적용 |
| v1-1 | [`lane_state_analysis_v1-1.ipynb`](lane_state_analysis_v1-1.ipynb) | 보충 셀 추가 (Run All 시 `NameError`가 발생하던 버전) |
| v2 | [`lane_state_analysis_v2.ipynb`](lane_state_analysis_v2.ipynb) | 셀 순서 · 미정의 변수 정리, 차선 미검출 시 `None` 처리, 전체 영상 실행 |

### 2단계 · 원본 시점 차선 검출

ROI + HLS · Canny + Hough

| 버전 | 파일 | 변경 내용 |
|:-:|---|---|
| v3 | [`lane_state_analysis_v3.ipynb`](lane_state_analysis_v3.ipynb) | 블랙박스 시점과 맞지 않는 고정 BEV 제거 |
| v4 | [`lane_state_analysis_v4.ipynb`](lane_state_analysis_v4.ipynb) | Optical Flow 상대 움직임 추가, Hough 배열 인덱싱 오류 수정 |
| v5 | [`lane_state_analysis_v5.ipynb`](lane_state_analysis_v5.ipynb) | 상태 변수 초기화 보강 → 이후 최종본으로 정리 |

v2의 결과 영상 출력(약 55MB)은 GitHub 파일 용량 제한으로 제거했고, 나머지 실행 결과는 그대로 남아 있습니다.
