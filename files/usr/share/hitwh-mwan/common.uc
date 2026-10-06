// Native LuCI/rpcd helpers. State samples and job results never read passwords.
'use strict';
import * as fs from 'fs';
import { cursor } from 'uci';

export function run(argv) {
    // 24.10's ucode predates popen(argv-array); quote only non-secret arguments.
    let command = join(' ', map(argv, value => "'" + replace(value, "'", "'\\''") + "'"));
    let p = fs.popen(command, 'r');
    if (!p) return { code: 127, output: '' };
    let output = p.read('all') || '';
    return { code: p.close(), output };
};

export function valid_interface(value) {
    return type(value) == 'string' && !!match(value, /^[a-zA-Z0-9_-]{1,32}$/);
};

export function valid_credentials(a) {
    return type(a.username) == 'string' && type(a.password) == 'string' &&
        length(a.username) > 0 && length(a.username) <= 1024 &&
        length(a.password) > 0 && length(a.password) <= 2048 &&
        index(a.username + a.password, chr(0)) < 0 && !match(a.username + a.password, /[[:cntrl:]]/);
};

export function settings() {
    let u = cursor();
    return {
        parent_device: u.get('hitwh_mwan', 'main', 'parent_device') || 'eth1',
        lan_device: u.get('hitwh_mwan', 'main', 'lan_device') || 'br-lan',
        main_interface: u.get('hitwh_mwan', 'main', 'main_interface') || 'wan',
        main_enrolled: u.get('hitwh_mwan', 'main', 'main_enrolled') != '0',
        portal_url: u.get('hitwh_mwan', 'main', 'portal_url') || 'http://172.26.156.158/eportal',
        health_url: u.get('hitwh_mwan', 'main', 'health_url') || 'http://connectivitycheck.gstatic.com/generate_204',
        balance_mode: u.get('hitwh_mwan', 'main', 'balance_mode') || 'round_robin',
        router_failover: u.get('hitwh_mwan', 'main', 'router_failover') != '0',
        max_paths: +(u.get('hitwh_mwan', 'main', 'max_paths') || 17)
    };
};

function numberfile(path) { return +(trim(fs.readfile(path) || '') || 0); }

export function snapshot() {
    let u = cursor(), config = settings(), paths = [], status = {};
    for (let line in split(fs.readfile('/tmp/hitwh-mwan.status') || '', '\n')) {
        let parts = split(trim(line), /\s+/);
        if (length(parts) < 2) continue;
        let row = {};
        for (let item in slice(parts, 1)) {
            let pair = split(item, '=');
            if (length(pair) == 2) row[pair[0]] = pair[1];
        }
        status[parts[0]] = row;
    }
    for (let n = 1; n <= config.max_paths; n++) {
        if (n == 6 || (n == 1 && !config.main_enrolled)) continue;
        let iface = n == 1 ? config.main_interface : 'wan' + n;
        let device = u.get('network', iface, 'device');
        if (!device || u.get('network', iface) != 'interface') continue;
        if (n != 1 && (u.get('network', 'wan' + n + 'dev', 'type') != 'macvlan' ||
            u.get('network', 'wan' + n + 'dev', 'ifname') != config.parent_device || device != 'macwan' + n ||
            u.get('network', iface, 'proto') != 'dhcp')) continue;
        let saved = fs.lstat('/etc/hitwh-mwan/auth.d/' + iface + '.json');
        let state = status[iface] || {};
        push(paths, {
            interface: iface, device, is_main: n == 1,
            mac: trim(fs.readfile('/sys/class/net/' + device + '/address') || '') ||
                u.get('network', n == 1 ? iface : 'wan' + n + 'dev', 'macaddr') || '—',
            ip: state.ip || 'none', state: state.state || 'no-dhcp',
            credentials_saved: !!saved && saved.type == 'file',
            rx_counter: numberfile('/sys/class/net/' + device + '/statistics/rx_bytes'),
            tx_counter: numberfile('/sys/class/net/' + device + '/statistics/tx_bytes')
        });
    }
    let cpu = [], memory = {};
    for (let value in slice(split(trim(split(fs.readfile('/proc/stat') || '', '\n')[0]), /\s+/), 1)) push(cpu, +value);
    for (let line in split(fs.readfile('/proc/meminfo') || '', '\n')) {
        let m = match(line, /^(MemTotal|MemAvailable):\s+(\d+)/);
        if (m) memory[m[1]] = +m[2];
    }
    return {
        raw: { timestamp: time(), clock: +(split(fs.readfile('/proc/uptime') || '0', ' ')[0]), cpu,
            memory_used_percent: memory.MemTotal ? 100 * (memory.MemTotal - memory.MemAvailable) / memory.MemTotal : 0,
            load: map(slice(split(trim(fs.readfile('/proc/loadavg') || '0 0 0'), /\s+/),0,3), x => +x),
            conntrack: { count: numberfile('/proc/sys/net/netfilter/nf_conntrack_count'),
                max: numberfile('/proc/sys/net/netfilter/nf_conntrack_max') }, paths },
        settings: config
    };
};

export function summarize(action, code, output) {
    let details = [], result = { ok: code == 0, state: 'done', action, details,
        message: code == 0 ? '操作完成' : '操作失败，请检查线路状态和配置' };
    for (let line in split(output || '', '\n')) {
        let auth = match(trim(line), /^AUTH_RESULT ([a-zA-Z0-9_-]{1,32}) ([a-z_]+)$/);
        if (auth) push(details, { interface: auth[1], code: auth[2] });
        let summary = match(trim(line), /^RESULT (\d+) (\d+) (\d+)$/);
        if (summary) { result.attempted = +summary[1]; result.recovered = +summary[2]; result.remaining = +summary[3]; }
        let created = match(line, /^(Added( and authenticated)?|Created|Updated|Removed) ([a-zA-Z0-9_-]{1,32})/);
        if (created) result.interface = created[3];
        if (match(line, /^Created /)) result.retained = true;
        if (match(line, /another WAN management operation/)) result.message = '另一项线路操作正在进行，请稍后重试';
        if (match(line, /no free WAN slot|maximum.*paths.*reached/)) result.message = '线路数量已达到上限';
    }
    return result;
};
