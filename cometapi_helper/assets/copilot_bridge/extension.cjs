'use strict';
const vscode = require('vscode');
const fs = require('node:fs');
const path = require('node:path');
exports.activate = async () => {
  const dir = process.env.COMETAPI_COPILOT_BRIDGE_DIRECTORY;
  if (!dir || !path.isAbsolute(dir)) return;
  let result;
  try {
    const stat = fs.lstatSync(dir), file = path.join(dir, 'request.json'), fstat = fs.lstatSync(file);
    if (!stat.isDirectory() || stat.isSymbolicLink() || (process.platform !== 'win32' && (stat.mode & 0o077)) ||
        !fstat.isFile() || fstat.isSymbolicLink() || fstat.nlink !== 1 || fstat.size > 1048576 || (process.platform !== 'win32' && (fstat.mode & 0o077)))
      throw Error('invalid-request');
    const request = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (request.operation !== 'configure' || !/^sk-[A-Za-z0-9_-]{8,250}$/.test(request.key)) throw Error('invalid-request');
    // The normal model-manager command enables the bundled BYOK provider for
    // a new profile. It does not sign in, accept agreements or alter trust.
    await vscode.commands.executeCommand('workbench.action.chat.manage');
    const copilot = vscode.extensions.getExtension('GitHub.copilot-chat');
    if (!copilot || typeof vscode.lm?.selectChatModels !== 'function') throw Error('unsupported');
    await copilot.activate();
    await vscode.commands.executeCommand('workbench.action.closeModalEditor');
    // Host has already removed only our prior group from the closed profile.
    await vscode.commands.executeCommand('lm.addLanguageModelsProviderGroup', {
      name: 'CometAPI Connect', vendor: 'customendpoint', apiKey: request.key,
      apiType: 'chat-completions', models: [request.model]
    });
    const models = await vscode.lm.selectChatModels({vendor: 'customendpoint', id: request.model.id});
    if (models.length !== 1 || models[0].name !== request.model.name) throw Error('model-not-unique');
    for (const setting of ['utilityModel', 'utilitySmallModel'])
      await vscode.workspace.getConfiguration('chat').update(setting, request.model.name + ' (customendpoint)', vscode.ConfigurationTarget.Global);
    await vscode.commands.executeCommand('workbench.action.chat.newChat');
    await vscode.commands.executeCommand('workbench.action.chat.open', {mode:'ask', modelSelector:{vendor:'customendpoint',id:request.model.id}});
    result = {ok:true, model:{id:models[0].id,vendor:models[0].vendor,name:models[0].name}};
  } catch (error) {
    result = {ok:false,error:['invalid-request','unsupported','model-not-unique'].includes(error.message) ? error.message : 'native-configuration-failed'};
  }
  fs.writeFileSync(path.join(dir,'response.tmp'),JSON.stringify(result),{mode:0o600,flag:'wx'});
  fs.renameSync(path.join(dir,'response.tmp'),path.join(dir,'response.json'));
};
