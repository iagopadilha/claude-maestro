/* Shared pieces of the repository role and folder choice. The by-hand management (add, switch role, remove) lives ONLY in the Workspace
   dialog (panel/workspace_dialog.js); the old hub/Workspace-bar "Repositories" dialog was removed. */
import { pickFolderNativeFirst } from '../components/folder_browser.js';

/** Plain role words and their hovers (RP-02), shared with the Workspace dialog (panel/workspace_dialog.js). Values sent to the server stay edit | reference. */
export const ROLE_TEXT = {
  edit: { label: 'edits', hover: 'The loop changes this repository: it creates its own worktree on a new loop branch and opens a PR at the end.' },
  reference: { label: 'reference', hover: 'Read only: the agents may read it to understand contracts or examples; the loop never changes it and creates no worktree.' },
};
export const roleKey = (r) => (r && r.role === 'reference' ? 'reference' : 'edit');

export async function chooseFolder(initial = '') {
  return pickFolderNativeFirst({ title: 'Repository folder', buttonText: 'Add this folder', initial }); // null = the window was cancelled
}
