@echo off
REM python ./main.py -i assets/imgs -o assets/outs -m custom_pairs -p ./assets/pairs.txt
REM python ./main.py -i assets/imgs -o assets/outs -m sequential -v 1
REM python ./main.py -i assets/imgs -o assets/outs -m sequential -f lightglue -n 8000 -v 1
python ./main.py -i assets/imgs -o assets/outs -m sequential -f detect_and_describe -n 8000 -v 1