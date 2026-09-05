'use strict';

const fs = require('node:fs');

function ucodeType(value) {
	if (value === null || value === undefined)
		return null;
	if (Array.isArray(value))
		return 'array';
	if (typeof value === 'number')
		return Number.isInteger(value) ? 'int' : 'double';
	if (typeof value === 'boolean')
		return 'bool';
	return typeof value;
}

function loadUcodeModule(path) {
	const source = fs.readFileSync(path, 'utf8');
	const factory = new Function(
		'type', 'length', 'keys', 'sort', 'match', 'push', 'substr', 'int',
		'index', 'replace', 'split', 'lc', 'sprintf',
		'json', 'join',
		source
	);
	return factory(
		ucodeType,
		value => typeof value === 'string' ? Buffer.byteLength(value, 'utf8') : value.length,
		value => Object.keys(value),
		value => value.sort(),
		(value, expression) => value.match(expression),
		(array, value) => array.push(value),
		(value, start, count) => count === undefined ? value.substring(start) : value.substring(start, start + count),
		value => Number.parseInt(value, 10),
		(value, needle) => value.indexOf(needle),
		(value, expression, replacement) => value.replace(expression, replacement),
		(value, separator) => value.split(separator),
		value => value.toLowerCase(),
		(format, value) => format === '%J' ? JSON.stringify(value) : (() => { throw new Error(`unsupported format ${format}`); })(),
		value => JSON.parse(value),
		(separator, value) => value.join(separator)
	);
}

module.exports = { loadUcodeModule };
