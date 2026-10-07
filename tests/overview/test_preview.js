// Exercise the real preview renderer without a browser or external packages.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname,
    '../../source/app/static/assets/js/iris/overview.js'), 'utf8');
const renderer = source.slice(source.indexOf('function render_case_view('),
    source.indexOf('$(document).ready('));

for (const owner of [null, {user_name: 'Assigned analyst'}]) {
    const displayed = [];
    let opened = false;
    const element = {
        find() { return this; }, empty() { return this; },
        addClass() { return this; }, append() { return this; },
        html() { return this; },
        text(value) { displayed.push(value); return this; },
        modal(action) { opened = action === 'show'; return this; },
    };
    const context = {
        $: () => element,
        sanitizeHTML: value => value,
        do_md_filter_xss: value => value,
        get_showdown_convert: () => ({makeHtml: value => value}),
    };
    vm.runInNewContext(renderer, context);
    context.render_case_view({
        case_id: 59, case_uuid: 'fixture-case', name: 'Unassigned Defender case',
        owner, user: {user_name: 'Opening analyst'}, tags: [], alerts: [],
        open_date: '2026-10-07', status_name: 'unspecified', soc_id: 'XDR-211166',
        client: {customer_name: 'Fixture customer'}, description: 'Fixture summary',
    });
    assert.equal(opened, true, 'The case preview must open');
    assert.ok(displayed.includes(owner ? owner.user_name : 'Unassigned'),
        'The preview must display the owner or an explicit unassigned label');
}
console.log('Case preview checks passed for unassigned and assigned owners.');
