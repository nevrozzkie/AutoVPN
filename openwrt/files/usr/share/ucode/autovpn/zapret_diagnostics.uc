'use strict';

const SCOPES = ['direct', 'vless', 'hysteria2'];
const CANDIDATES = {
	direct: ['direct:blockcheck-fake', 'direct:blockcheck-fake-multisplit'],
	vless: ['vless:split', 'vless:multidisorder', 'vless:fake_multidisorder', 'vless:fake_multisplit'],
	hysteria2: ['hysteria2:fake', 'hysteria2:fake_plain', 'hysteria2:fake11'],
};

function validScope(value) { return type(value) == 'string' && index(SCOPES, value) >= 0; }
function candidates(value) { return validScope(value) ? json(sprintf('%J', CANDIDATES[value])) : []; }
function validCandidate(scope, value) { return validScope(scope) && type(value) == 'string' && index(CANDIDATES[scope], value) >= 0; }

return { scopes: json(sprintf('%J', SCOPES)), validScope: validScope,
	candidates: candidates, validCandidate: validCandidate };
