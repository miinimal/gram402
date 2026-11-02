// Node's Buffer is only used as a fallback when btoa/atob are missing; declared here to keep the package dependency-free.
declare const Buffer: { from(data: string, encoding?: string): { toString(encoding?: string): string } };
