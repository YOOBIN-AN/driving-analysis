# course_lib/

> 출처: 경북대학교 전자공학부 ACE Lab 하계 코딩캠프 제공 자율주행 실습 코드 (`ros3_ws`)
> 수업 자료라 **이 저장소에는 포함하지 않았습니다.**

최종 노트북의 1 ~ 10절(수업 파이프라인 재현)은 아래 두 파일을 이 폴더에 두어야 실행됩니다.

| 파일 | 원래 위치 | 노트북에서 사용한 함수 |
|---|---|---|
| `camera_perception_func_lib.py` | `ros3_ws/src/camera_perception_pkg/camera_perception_pkg/lib/` | `bird_convert`, `roi_rectangle_below`, `dominant_gradient`, `get_lane_center` |
| `decision_making_function_lib.py` | `ros3_ws/src/decision_making_pkg/decision_making_pkg/lib/` | `calculate_slope_between_points` |

블랙박스 영상 분석 스크립트 [`src/driveguard.py`](../driveguard.py)는 이 라이브러리 없이 단독으로 실행됩니다.
