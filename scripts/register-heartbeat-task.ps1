$ErrorActionPreference = 'Stop'
$Vbs = 'C:\Users\obing\Documents\GitHub\VAMS-frontend\docs\session-handoff\mission-control\pages\scripts\heartbeat_hidden.vbs'
$TaskName = 'VAMS-MissionControl-Heartbeat'
# 2026-07-24 STALENESS INCIDENT #2 fix: PT1M repeat was firing FASTER than the real observed
# end-to-end cycle time under fleet load (gen_stats + git add/commit/push regularly ran 2-3.5 min
# even after the ROSTER_BUDGET_SEC fix). Every re-trigger while the previous wscript instance was
# still alive got REJECTED at the Windows Task Scheduler level by MultipleInstances=IgnoreNew,
# which recorded LastTaskResult=0x800710E0 ("operator refused") -- masking whether the underlying
# script was actually healthy (it was: the script's OWN mkdir single-writer lock always let the
# real work complete and push cleanly; see .heartbeat.log). Widening the trigger to PT2M (still a
# "sane ~1-2 min" cadence per the dashboard's freshness doctrine, and well under STALE_MIN=2/
# IDLE_PULSE_MIN=15 in gen_stats.py's own push-throttle policy) cuts collision frequency sharply so
# LastTaskResult reflects real completions again. ExecutionTimeLimit bumped 3->4 min to match
# sysres's own margin, giving headroom above the measured ~2-3.5 min real-world worst case.
$Action  = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('//B //Nologo "{0}"' -f $Vbs)
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date)
$Trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 2) `
    -RepetitionDuration (New-TimeSpan -Days 9999)).Repetition
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -Priority 4 `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Write-Output ("registered '{0}': every 2 min, windowless (wscript), ExecutionTimeLimit=PT4M, Priority=4, MultipleInstances=IgnoreNew" -f $TaskName)
