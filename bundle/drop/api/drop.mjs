// Vercel entry: /d/<token> is rewritten here (vercel.json). Private Vercel Blob store.
import { put, get, del, list } from '@vercel/blob';
import { handle, blobStore } from '../core.mjs';

const store = blobStore({ put, get, del, list });
const run = (request) => handle(request, { store, env: process.env });
export const GET = run;
export const PUT = run;
export const DELETE = run;
export const POST = run;
