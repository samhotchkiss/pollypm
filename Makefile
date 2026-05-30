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
	POLLYPM_PERF_SCHEMA=$${POLLYPM_PERF_SCHEMA:-pollypm_perf_m} \
	POLLYPM_PERF_WORKSPACE=$${POLLYPM_PERF_WORKSPACE:-/tmp/pollypm-perf-m} \
	sh -c 'trap "python3 scripts/perf/seed_scale.py teardown --scale m" EXIT; scripts/perf/seed_mscale.sh --force-clean --execute; scripts/perf/measure_http.sh --scenarios dashboard,sessions,messages,task-list,task-detail,inbox'
