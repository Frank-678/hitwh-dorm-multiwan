'use strict';
import { readfile, open } from 'fs';
import { summarize } from './common.uc';
let dir = ARGV[0], action = ARGV[1], code = ARGV[2];
let output = readfile(dir + '/output', 16384);
let result = summarize(action, +code, output);
if (action == 'discover' && +code == 0) {
    try { result.portal_url = json(output).portal_url; }
    catch (e) { result.ok = false; result.message = '接口当前已在线，未检测到认证跳转；请填写学校提供的门户地址'; }
}
let f = open(dir + '/result.json', 'w', 0600);
if (!f) exit(1);
f.write(sprintf('%J', result)); f.close();
