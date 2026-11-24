"""数据管线脚本包（fetch → clean → validate → import，全部幂等）。

T01 仅占位；具体脚本在 T02/T03/T04 落地：
- fetch_score_segments.py / clean_score_segments.py   一分一段
- fetch_admission.py / clean_admission.py             投档线
- annotate_university_meta.py                         985/211/双一流标注
- annotate_subject_requirement.py                      选科要求提取（缺失标「待补」）
- validate_data.py                                     数据校验
- spotcheck_admission.py                               独立抽查（不复用清洗结果）
- import_data.py                                       幂等导入（MySQL）
"""
