'use strict';
import { stdin } from 'fs';
import { cursor } from 'uci';
import { settings } from './common.uc';
let a;
try { a = json(stdin.read('all')); } catch (e) { exit(2); }
let u = cursor(), old = settings();
for (let key in ['parent_device', 'lan_device'])
    if (!match(a[key] || '', /^[a-zA-Z0-9_.:-]{1,32}$/)) exit(2);
for (let key in ['portal_url', 'health_url'])
    if (!match(a[key] || '', /^https?:\/\/[^[:space:]@?#]+(\/[^[:space:]?#]*)?$/) || length(a[key]) > 512) exit(2);
if (a.parent_device != old.parent_device) {
    let occupied = old.main_enrolled;
    u.foreach('network', 'interface', s => { if (match(s['.name'], /^wan[0-9]+$/)) occupied = true; });
    if (occupied) exit(3);
}
if (index(['round_robin', 'random'], a.balance_mode) < 0 || index(['0','1'],a.router_failover) < 0) exit(2);
for (let key in ['parent_device', 'lan_device', 'portal_url', 'health_url','balance_mode','router_failover']) u.set('hitwh_mwan', 'main', key, a[key]);
if (!u.commit('hitwh_mwan')) exit(1);
