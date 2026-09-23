'use strict';

/**
 * A job queue that runs at most `concurrency` jobs at once. `push(job)` takes
 * a function returning a promise, and returns a promise for its result.
 */
function createQueue(concurrency) {
  if (!(concurrency >= 1)) throw new RangeError('concurrency must be at least 1');
  let active = 0;
  const waiting = [];

  function next() {
    if (active > concurrency || waiting.length === 0) return;
    const { job, resolve, reject } = waiting.shift();
    active += 1;
    Promise.resolve()
      .then(job)
      .then(resolve, reject)
      .finally(() => {
        active -= 1;
        next();
      });
    next();
  }

  return {
    push(job) {
      return new Promise((resolve, reject) => {
        waiting.push({ job, resolve, reject });
        next();
      });
    },
    get active() {
      return active;
    },
  };
}

module.exports = { createQueue };
