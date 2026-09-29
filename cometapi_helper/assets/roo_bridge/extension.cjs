'use strict';
exports.activate = async () => {
  if (process.env.COMETAPI_ROO_BRIDGE_DIRECTORY) await require('./runner.cjs').run();
};
