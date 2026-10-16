/* Single entry point for Preact (vendored in /static/vendor, resolved by the import map in index.html).
   No npm and no build step: modules import html/render/hooks from here, never from 'preact' directly. */
export { h, render, Fragment, createRef, Component } from 'preact';
export { useState, useEffect, useLayoutEffect, useRef, useMemo, useCallback, useReducer } from 'preact/hooks';
export { html } from 'htm/preact';
