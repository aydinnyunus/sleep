rem aydinnyunus-sleep-nightly
rem One unattended pass a day at 03:41 via Windows Task Scheduler.
rem Report-only: `nightly` runs the gated consolidation and a weekly replay report.
rem Promotion stays a human running `sleepctl promote`.
rem Install via:  python3 tools\install.py --nightly
rem Manual:       schtasks /create /tn aydinnyunus-sleep-nightly /tr "<cmd>" /sc daily /st 03:41 /f
rem Check:        schtasks /query /tn aydinnyunus-sleep-nightly
rem Remove:       schtasks /delete /tn aydinnyunus-sleep-nightly /f
rem
rem __SLEEP_SCHTASKS_CMD__ is inserted by install.py with the repo path substituted.
