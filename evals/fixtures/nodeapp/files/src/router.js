'use strict';

/** A tiny request router: exact paths only. */
function createRouter() {
  const routes = [];
  return {
    add(method, path, handler) {
      routes.push({ method, path, handler });
      return this;
    },
    /** The matching route as { handler, params }, or null. */
    match(method, path) {
      const route = routes.find((r) => r.method === method && r.path === path);
      return route ? { handler: route.handler, params: {} } : null;
    },
  };
}

module.exports = { createRouter };
