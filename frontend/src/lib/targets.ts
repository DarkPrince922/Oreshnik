/** Targets cannot contain whitespace, so any run of whitespace or commas separates them. */
export const parseTargets = (text: string): string[] => text.split(/[\s,]+/).filter(Boolean);
