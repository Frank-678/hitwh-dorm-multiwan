'use strict';
import * as fs from 'fs';
import { settings, snapshot, run, valid_interface, valid_credentials } from '/usr/share/hitwh-mwan/common.uc';

function credential_choices() {
    // Called only by an explicit credential dialog. Passwords stay within this
    // privileged process while grouping; the response contains labels only.
    let groups = [];
    for (let path in snapshot().raw.paths) {
        if (!path.credentials_saved) continue;
        let r = run(['/usr/sbin/hitwh-mwan', 'auth', 'get', path.interface]);
        if (r.code) continue;
        try {
            let data = json(r.output);
            if (!valid_credentials(data)) continue;
            let group = null;
            for (let candidate in groups)
                if (candidate.username == data.username && candidate.password == data.password) { group = candidate; break; }
            if (group) push(group.interfaces, path.interface);
            else push(groups, { username: data.username, password: data.password, interfaces: [path.interface] });
        } catch (e) {}
    }
    return { ok: true, choices: map(groups, g => ({ interface: g.interfaces[0], username: g.username, interfaces: g.interfaces })) };
}

function start(request) {
    let a = request.args, actions = ['add-random', 'add-mac', 'refresh', 'remove', 'edit',
        'credentials-save', 'credentials-remove', 'configure', 'discover', 'upgrade'];
    if (index(actions, a.action) < 0) return { ok: false, message: '无效操作' };
    if (index(['remove', 'edit', 'credentials-save', 'credentials-remove'], a.action) >= 0 && !valid_interface(a.interface))
        return { ok: false, message: '无效线路' };
    if (a.action == 'refresh' && a.interface && !valid_interface(a.interface)) return { ok: false, message: '无效线路' };
    if (a.action == 'discover') a.interface = settings().main_interface;
    if (index(['add-mac', 'edit'], a.action) >= 0 && !match(a.mac || '', /^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$/))
        return { ok: false, message: '请输入有效 MAC 地址' };
    if (index(['add-random', 'credentials-save'], a.action) >= 0 && !valid_credentials(a))
        return { ok: false, message: '请输入有效账号和密码' };
    // Bound ephemeral job storage. Only completed/expired private jobs are removed.
    let jobs = fs.glob('/tmp/hitwh-mwan-job.*') || [];
    for (let dir in jobs) {
        let s = fs.lstat(dir);
        if (!s || s.type != 'directory' || s.uid != 0 || s.mode != 0700) continue;
        if (time() - s.mtime > 600 && !fs.access(dir + '/pid')) {
            for (let name in ['request.json','result.json','output','pid','started','worker','common.uc','job-result.uc']) fs.unlink(dir + '/' + name);
            fs.rmdir(dir);
        }
    }
    if (length(fs.glob('/tmp/hitwh-mwan-job.*') || []) >= 32) return { ok: false, message: '临时操作已达上限，请稍后重试' };
    let created = run(['/bin/sh', '-c', 'umask 077; mktemp -d /tmp/hitwh-mwan-job.XXXXXX']);
    let dir = trim(created.output);
    if (created.code || !match(dir, /^\/tmp\/hitwh-mwan-job\.[a-zA-Z0-9]{6}$/)) return { ok: false, message: '无法创建操作' };
    fs.chmod(dir, 0700);
    let f = fs.open(dir + '/request.json', 'wx', 0600);
    if (!f) { fs.rmdir(dir); return { ok: false, message: '无法保存操作' }; }
    f.write(sprintf('%J', a)); f.close();
    // Keep the worker/modules stable if an upgrade replaces package files.
    for (let name in ['worker','common.uc','job-result.uc']) {
        let source = name == 'worker' ? '/usr/libexec/hitwh-mwan-job' : '/usr/share/hitwh-mwan/' + name;
        let data = fs.readfile(source), copy = fs.open(dir + '/' + name, 'wx', 0600);
        if (!data || !copy) {
            if (copy) copy.close();
            for (let old in ['request.json','worker','common.uc','job-result.uc']) fs.unlink(dir + '/' + old);
            fs.rmdir(dir); return { ok: false, message: '无法准备操作组件' };
        }
        copy.write(data); copy.close();
    }
    let rc = system('setsid /bin/sh ' + dir + '/worker ' + dir + ' </dev/null >/dev/null 2>&1 &');
    if (rc) {
        for (let name in ['request.json','worker','common.uc','job-result.uc']) fs.unlink(dir + '/' + name);
        fs.rmdir(dir); return { ok: false, message: '无法启动操作' };
    }
    return { ok: true, job: substr(dir, length('/tmp/hitwh-mwan-job.')) };
}

return { 'hitwh.mwan': {
    snapshot: { call: () => snapshot() },
    settings: { call: () => ({ ok: true, settings: settings() }) },
    credential_choices: { call: credential_choices },
    credentials: { args: { interface: '' }, call: function(request) {
        let iface = request.args.interface;
        if (!valid_interface(iface)) return { ok: false, message: '无效线路' };
        let r = run(['/usr/sbin/hitwh-mwan', 'auth', 'get', iface]);
        if (r.code) return { ok: false, message: '无法读取凭据，请检查线路及文件权限' };
        try {
            let data = json(r.output);
            return { ok: true, configured: data.configured != false,
                username: data.username || '', password: data.password || '', mac: data.mac || '' };
        } catch (e) { return { ok: false, message: '凭据文件格式无效' }; }
    } },
    start: { args: { action: '', interface: '', mac: '', username: '', password: '',
        parent_device: '', lan_device: '', portal_url: '', health_url: '', balance_mode: '', router_failover: '' }, call: start },
    job: { args: { id: '' }, call: function(request) {
        let id = request.args.id;
        if (!match(id || '', /^[a-zA-Z0-9]{6}$/)) return { ok: false, state: 'done', message: '无效操作编号' };
        let dir = '/tmp/hitwh-mwan-job.' + id, s = fs.lstat(dir);
        if (!s || s.type != 'directory' || s.uid != 0 || s.mode != 0700)
            return { ok: false, state: 'done', message: '操作已过期' };
        let result = fs.readfile(dir + '/result.json', 8192);
        if (result) { try { return json(result); } catch (e) {} }
        if (time() - s.mtime > 310 && !fs.access(dir + '/pid')) return { ok: false, state: 'done', message: '操作超时，请检查线路状态' };
        return { ok: true, state: 'running' };
    } }
} };
