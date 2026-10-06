'use strict';
'require view';
'require rpc';

const snapshot = rpc.declare({ object: 'hitwh.mwan', method: 'snapshot' });
const settings = rpc.declare({ object: 'hitwh.mwan', method: 'settings' });
const credentials = rpc.declare({ object: 'hitwh.mwan', method: 'credentials', params: ['interface'] });
const start = rpc.declare({ object: 'hitwh.mwan', method: 'start', params:
    ['action','interface','mac','username','password','parent_device','lan_device','portal_url','health_url','balance_mode','router_failover'] });
const job = rpc.declare({ object: 'hitwh.mwan', method: 'job', params: ['id'] });

return view.extend({
    render: function() {
        const frame = E('iframe', { src: L.resource('hitwh-mwan/index.html'), title: '多路合并管理器',
            style: 'width:100%;height:calc(100vh - 160px);min-height:700px;border:0;border-radius:10px' });
        const listener = (event) => {
            if (event.origin !== location.origin || event.source !== frame.contentWindow || event.data?.type !== 'hitwh-ready') return;
            const channel = new MessageChannel();
            channel.port1.onmessage = async ({data}) => {
                if (data.type === 'height') { frame.style.height = `${Math.min(20000, Math.max(700, Number(data.value) || 700))}px`; return; }
                let result;
                try {
                    const a = data.args || {};
                    if (data.method === 'snapshot') result = await snapshot();
                    else if (data.method === 'settings') result = await settings();
                    else if (data.method === 'credentials') result = await credentials(a.interface);
                    else if (data.method === 'job') result = await job(a.id);
                    else if (data.method === 'start') result = await start(a.action, a.interface || '', a.mac || '',
                        a.username || '', a.password || '', a.parent_device || '', a.lan_device || '', a.portal_url || '', a.health_url || '', a.balance_mode || '', a.router_failover || '');
                    else result = {ok:false,message:'无效请求'};
                } catch (error) { result = {ok:false,message:'路由器请求失败，请检查登录状态和组件'}; }
                channel.port1.postMessage({id:data.id,result});
            };
            frame.contentWindow.postMessage({type:'hitwh-connected'}, location.origin, [channel.port2]);
        };
        window.addEventListener('message', listener);
        return frame;
    },
    handleSave: null, handleSaveApply: null, handleReset: null
});
