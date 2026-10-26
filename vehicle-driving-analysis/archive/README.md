# archive

최종본 [`notebooks/lane_state_analysis_final.ipynb`](../notebooks/lane_state_analysis_final.ipynb)에 이르기까지의 개발 노트북입니다.

**수업 함수 기반 차선 검출** (BEV → `dominant_gradient` → `get_lane_center`)

- `lane_state_analysis_v1.ipynb` — 수업 파이프라인을 블랙박스 영상 프레임에 그대로 적용
- `lane_state_analysis_v1-1.ipynb` — 보충 셀 추가, Run All 시 `NameError`가 발생하던 버전
- `lane_state_analysis_v2.ipynb` — 셀 순서 · 미정의 변수 정리, 차선 미검출 시 `None` 처리, 전체 영상 실행

**원본 시점 차선 검출** (ROI + HLS + Canny + Hough)

- `lane_state_analysis_v3.ipynb` — 블랙박스 시점과 맞지 않는 고정 BEV 제거
- `lane_state_analysis_v4.ipynb` — Optical Flow 상대 속도 추가, Hough 배열 인덱싱 오류 수정
- `lane_state_analysis_v5.ipynb` — 상태 변수 초기화 보강 → 실행 후 최종본

> v2의 결과 영상 출력(약 55MB)은 GitHub 파일 용량 제한으로 제거했습니다. 나머지 실행 결과는 그대로입니다.
