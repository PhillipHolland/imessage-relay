// Vercel Cron (every minute, vercel.json): deletes anything older than 10 minutes that nobody fetched.
import { put, get, del, list } from '@vercel/blob';
import { handleSweep, blobStore } from '../core.mjs';

const store = blobStore({ put, get, del, list });
export const GET = (request) => handleSweep(request, { store, env: process.env });
