export declare function svgToPngDataUrl(svg: any, { scale, background }?: {
    background?: string;
    scale?: number;
}): Promise<{
    url: string;
    w: number;
    h: number;
}>;
