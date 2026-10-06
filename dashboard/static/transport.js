/* Shared UI: authenticated LuCI RPC bridge on router, HTTP API on desktop. */
(() => {
  let port, nextId = 0;
  const pending = new Map();
  const native = location.pathname.includes('/luci-static/');
  let connect;
  const connected = new Promise(resolve => { connect = resolve; });
  if (native) {
    window.addEventListener('message', event => {
      if (event.origin !== location.origin || event.source !== parent || event.data?.type !== 'hitwh-connected' || !event.ports[0]) return;
      port = event.ports[0];
      port.onmessage = ({data}) => {
        const entry = pending.get(data.id);
        if (!entry) return;
        pending.delete(data.id); clearTimeout(entry.timer); entry.resolve(data.result);
      };
      connect();
    });
    parent.postMessage({type:'hitwh-ready'}, location.origin);
  }
  async function call(method, args = {}) {
    if (!port) await Promise.race([connected, new Promise((_, reject) => setTimeout(() => reject(new Error('请登录 OpenWrt 并从“网络 → 多路合并管理器”打开页面')), 10000))]);
    return new Promise((resolve, reject) => {
      const id = ++nextId;
      const timer = setTimeout(() => { pending.delete(id); reject(new Error('路由器响应超时')); }, 15000);
      pending.set(id, {resolve, timer}); port.postMessage({id,method,args});
    });
  }
  async function action(args) {
    const started = await call('start', args);
    if (!started.ok) return started;
    const deadline = Date.now() + (args.action === 'upgrade' ? 480000 : 320000);
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 700));
      const result = await call('job', {id:started.job});
      if (result.state === 'done') return result;
    }
    return {ok:false,message:'操作超时，请检查线路状态'};
  }
  window.MwanAPI = {
    native,
    async request(path, options = {}) {
      if (!native) return fetch(path, {...options, cache:'no-store'});
      const data = options.body ? JSON.parse(options.body) : {};
      let result;
      if (path === '/api/snapshot') result = await call('snapshot');
      else if (path === '/api/config' && !options.body) result = await call('settings');
      else if (path === '/api/config') result = await action({action:'configure',...data});
      else if (path === '/api/reconnect') result = await action({action:'refresh',...data});
      else if (path === '/api/paths') result = await action(data);
      else if (path === '/api/credentials' && data.action === 'choices') result = await call('credential_choices');
      else if (path === '/api/credentials') result = data.action === 'get'
        ? await call('credentials', {interface:data.interface})
        : await action({...data,action:data.action === 'save' ? 'credentials-save' : 'credentials-remove'});
      else result = {ok:false,message:'无效请求'};
      return {ok:result.ok !== false, status:result.ok === false ? 409 : 200, json:async () => result};
    }
  };
})();
