@echo off  
cd /d d:\future  
python d:\future\scripts\training\run_train_batch.py --batch-dir d:\future\batch_plans\g2026.1 --execute --timeout-seconds 7200 > d:\future\batch_plans\g2026.1\training_log.txt 2>&1  
exit  
