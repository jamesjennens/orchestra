#!/bin/bash
# One mutant at a time; delivered tests only (reviewer tests removed from the copy).
R=/home/james/orchestra-review-evidence/1023-388r3/rev
S=/dev/shm/orc-review-388r3
OUT=$R/ev/mutants.txt
: > $OUT
for n in $(python3 -I $R/ev/mut388r3.py x list); do
  rm -rf /dev/shm/orc-review-388r3/m2; cp -a $S/m $S/m2
  python3 -I $R/ev/mut388r3.py $S/m2 $n >> $OUT 2>&1 || { echo "$n APPLY-FAILED" >> $OUT; continue; }
  $R/ev/run.sh $S/m2 $S/mt_fast.txt tests.test_rooms tests.test_rooms_http tests.test_rooms_items_http tests.test_key_projects tests.test_record_json
  fast=$(grep -E "^(Ran|OK|FAILED)" $S/mt_fast.txt | tr "\n" " ")
  if grep -q "^OK" $S/mt_fast.txt; then
    $R/ev/run.sh $S/m2 $S/mt_slow.txt tests.test_rooms_items_real_bd tests.test_capabilities
    slow=$(grep -E "^(Ran|OK|FAILED)" $S/mt_slow.txt | tr "\n" " ")
    if grep -q "^OK" $S/mt_slow.txt; then v=SURVIVED; else v=KILLED-slow; grep -E "^(FAIL|ERROR):" $S/mt_slow.txt | head -4 >> $OUT; fi
  else
    slow=-; v=KILLED-fast; grep -E "^(FAIL|ERROR):" $S/mt_fast.txt | head -4 >> $OUT
  fi
  echo "$n $v | fast: $fast | slow: $slow" >> $OUT
done
rm -rf /dev/shm/orc-review-388r3/m2
echo DONE >> $OUT
