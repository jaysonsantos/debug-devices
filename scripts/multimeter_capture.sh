#!/usr/bin/env bash
# Capture webcam frames of the multimeter for the accuracy check in docs/qa.md.
#
#   scripts/multimeter_capture.sh <out-dir> [count]
#
# It captures one frame each time you press Enter, and it writes <out-dir>/readings.csv
# with one empty row per frame. A human fills the columns that end in "_human".
set -euo pipefail

readonly DEFAULT_COUNT=20
readonly DEVICE="${DEBUG_DEVICES_WEBCAM:-/dev/video0}"
readonly VIDEO_SIZE="${QA_WEBCAM_SIZE:-1920x1080}"
readonly INPUT_FORMAT="${QA_WEBCAM_FORMAT:-mjpeg}"
# The first frames of a UVC camera are dark while the exposure settles.
readonly WARMUP_FRAMES="${QA_WEBCAM_WARMUP_FRAMES:-10}"
readonly CSV_HEADER="file,value_human,unit_human,mode_human,range_human,note"

if [[ $# -lt 1 ]]; then
  sed -n '2,7p' "$0"
  exit 2
fi

out_dir="$1"
count="${2:-$DEFAULT_COUNT}"
mkdir -p "$out_dir"
csv="$out_dir/readings.csv"
[[ -f "$csv" ]] || echo "$CSV_HEADER" >"$csv"

for ((i = 1; i <= count; i++)); do
  file="$(printf 'frame_%03d.jpg' "$i")"
  read -r -p "[$i/$count] set the meter, then press Enter to capture $file "
  ffmpeg -hide_banner -loglevel error -y \
    -f v4l2 -input_format "$INPUT_FORMAT" -video_size "$VIDEO_SIZE" -i "$DEVICE" \
    -vf "select=gte(n\\,$WARMUP_FRAMES)" -frames:v 1 "$out_dir/$file"
  echo "$file,,,,," >>"$csv"
done

echo "done: fill in $csv"
