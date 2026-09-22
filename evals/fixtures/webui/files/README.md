# webui

A status badge, rendered as a string. No framework, no build step.

    npm test      # node --test test/*.test.js
    npm run lint  # a syntax pass over src/

`src/badge.js` renders the markup, `src/badge.css` styles it. The two are
meant to stay in step: a state added to one needs a rule in the other.
