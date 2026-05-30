.PHONY: smoke perf-measure perf-poll perf-snapshot perf-mscale

smoke:
	python scripts/smoke.py

perf-measure:
	scripts/perf/measure_http.sh --scenarios dashboard,sessions,messages,task-list,task-detail,inbox

perf-poll:
	python3 scripts/perf/measure_http.py poll

perf-snapshot:
	python3 scripts/perf/measure_http.py resources

perf-mscale:
	uv run python scripts/perf/run_scale_gate.py --scale m
