import json

with open("scripts/calibration_report.json") as f:
    report = json.load(f)

pos = report["positive_pairs"]
synth = report["synthetic_edits"]
all_true = pos + synth  # true_sim/top1_is_correct 공통 필드 사용

print(f"total ground-truth cases (real+synthetic): {len(all_true)}")
correct = [c for c in all_true if c["top1_is_correct"]]
wrong = [c for c in all_true if not c["top1_is_correct"]]
print(f"top1 correct: {len(correct)} ({len(correct)/len(all_true)*100:.1f}%)")
print(f"top1 WRONG (mismatched to different original): {len(wrong)} ({len(wrong)/len(all_true)*100:.1f}%)")
for w in wrong:
    print(f"  MISMATCH: project={w.get('project')} edit={w.get('edit_type', w.get('version_filename'))} true_sim={w['true_sim']} top1_sim={w['top1_sim']} gap={round(w['top1_sim']-w['true_sim'],4)}")

print("\n--- true_sim distribution (all ground-truth, regardless of correctness) ---")
sims = sorted(c["true_sim"] for c in all_true)
print("min", sims[0], "p25", sims[len(sims)//4], "median", sims[len(sims)//2], "p75", sims[3*len(sims)//4], "max", sims[-1])

print("\n--- candidate threshold scenarios ---")
for auto, low in [(0.96, 0.85), (0.95, 0.85), (0.93, 0.80)]:
    n_auto = sum(1 for c in all_true if c["true_sim"] >= auto)
    n_auto_correct = sum(1 for c in all_true if c["true_sim"] >= auto and c["top1_is_correct"])
    n_low = sum(1 for c in all_true if low <= c["true_sim"] < auto)
    n_low_correct = sum(1 for c in all_true if low <= c["true_sim"] < auto and c["top1_is_correct"])
    n_below = sum(1 for c in all_true if c["true_sim"] < low)
    print(f"AUTO={auto} LOW={low}: gemini band n={n_auto} (correct={n_auto_correct}, wrong={n_auto-n_auto_correct}) | "
          f"gemini_low band n={n_low} (correct={n_low_correct}, wrong={n_low-n_low_correct}) | below LOW (unmatched) n={n_below}")

print("\n--- burst (negative) cross-similarity vs positive true_sim overlap ---")
for b in report["burst_cross_similarity"]:
    print(b)

print("\n--- margin (true_sim - top1_sim when top1 is wrong) ---")
for w in wrong:
    print(f"  {w.get('project')} {w.get('edit_type', w.get('version_filename'))}: true_rank={w['true_rank']} gap={round(w['true_sim']-w['top1_sim'],4)}")

print("\n--- margin (top1_top2_margin) distribution among CORRECT cases ---")
margins = sorted(c["top1_top2_margin"] for c in correct if c["top1_top2_margin"] is not None)
print("min", margins[0], "p25", margins[len(margins)//4], "median", margins[len(margins)//2], "max", margins[-1])
