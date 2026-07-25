export function svgToPngDataUrl(svg: any, { scale, background }?: {
    scale?: number;
    background?: string;
}): Promise<{
    url: string;
    w: number;
    h: number;
}>;
