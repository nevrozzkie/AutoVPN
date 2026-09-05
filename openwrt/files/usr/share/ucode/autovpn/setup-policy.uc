'use strict';

/* Pure first-run guard so its destructive-boundary cases are testable off-device. */
function firstRunAllowed(state) {
	if (state.uci_clean !== true || state.enabled === true || state.controller_journal === true)
		return { ok: false, code: 'existing_installation' };
	if (state.network_phase == 'pending' || state.network_invalid === true)
		return { ok: false, code: 'existing_installation' };
	if (state.prepared === true) {
		if (state.identity_matches !== true) return { ok: false, code: 'setup_identity_locked' };
		return { ok: true, retry: true };
	}
	return (state.network_phase == null || (state.bootstrap_confirmed === true && state.network_phase == 'confirmed')) &&
		state.credential_present !== true
		? { ok: true, retry: false }
		: { ok: false, code: 'existing_installation' };
}

return { firstRunAllowed: firstRunAllowed };
