// Which files the studio accepts as a source. ffmpeg decodes far more than
// this; the list only decides what the picker offers and what a drop is
// allowed to fill the box with.
export const VIDEO_EXTENSIONS = [
  'mp4', 'mov', 'm4v', 'mkv', 'webm', 'avi', 'mpg', 'mpeg', 'mts', 'm2ts', 'wmv', 'flv'
]

export const isVideoPath = (path: string): boolean => {
  const match = /\.([^./\\]+)$/.exec(path)
  return !!match && VIDEO_EXTENSIONS.includes(match[1].toLowerCase())
}

/** First video in a drop (a folder or a mixed selection can carry other things). */
export const pickVideo = (paths: string[]): string | null => paths.find(isVideoPath) ?? null
