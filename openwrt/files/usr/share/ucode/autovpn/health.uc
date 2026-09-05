'use strict';

function initialHealth() {
	return {
		status: 'unknown',
		consecutive_failures: 0,
		consecutive_successes: 0,
		cooldown_until: 0,
		last_probe_at: 0,
	};
}

function observe(state, success, now, settings) {
	state.last_probe_at = now;
	if (success === true) {
		state.consecutive_successes += 1;
		state.consecutive_failures = 0;
		if (state.consecutive_successes >= settings.success_threshold && now >= state.cooldown_until)
			state.status = 'up';
	}
	else {
		state.consecutive_failures += 1;
		state.consecutive_successes = 0;
		if (state.consecutive_failures >= settings.failure_threshold) {
			state.status = 'down';
			state.cooldown_until = now + settings.cooldown_seconds;
		}
	}
	return state;
}

function selectOutbound(candidates, health, manualOverride) {
	if (manualOverride != null && manualOverride != '' && manualOverride != 'auto') {
		for (let i = 0; i < length(candidates); i++)
			if (candidates[i] == manualOverride)
				return health[manualOverride]?.status == 'up' ? manualOverride : null;
		return null;
	}
	for (let i = 0; i < length(candidates); i++)
		if (health[candidates[i]]?.status == 'up')
			return candidates[i];
	return null;
}

return {
	initialHealth: initialHealth,
	observe: observe,
	selectOutbound: selectOutbound,
};
