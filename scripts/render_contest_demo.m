#import <AVFoundation/AVFoundation.h>
#import <CoreGraphics/CoreGraphics.h>
#import <Foundation/Foundation.h>
#import <ImageIO/ImageIO.h>
#import <VideoToolbox/VideoToolbox.h>

static CGImageRef LoadImage(NSURL *url) {
    CGImageSourceRef source = CGImageSourceCreateWithURL((__bridge CFURLRef)url, NULL);
    if (!source) return NULL;
    CGImageRef image = CGImageSourceCreateImageAtIndex(source, 0, NULL);
    CFRelease(source);
    return image;
}

static CVPixelBufferRef MakePixelBuffer(
    NSInteger width,
    NSInteger height,
    CGImageRef first,
    CGImageRef second,
    CGFloat progress
) {
    NSDictionary *attributes = @{
        (__bridge NSString *)kCVPixelBufferCGImageCompatibilityKey: @YES,
        (__bridge NSString *)kCVPixelBufferCGBitmapContextCompatibilityKey: @YES,
        (__bridge NSString *)kCVPixelBufferPixelFormatTypeKey: @(kCVPixelFormatType_32ARGB),
        (__bridge NSString *)kCVPixelBufferWidthKey: @(width),
        (__bridge NSString *)kCVPixelBufferHeightKey: @(height),
    };
    CVPixelBufferRef buffer = NULL;
    CVReturn status = CVPixelBufferCreate(
        kCFAllocatorDefault,
        width,
        height,
        kCVPixelFormatType_32ARGB,
        (__bridge CFDictionaryRef)attributes,
        &buffer
    );
    if (status != kCVReturnSuccess || !buffer) return NULL;

    CVPixelBufferLockBaseAddress(buffer, 0);
    void *baseAddress = CVPixelBufferGetBaseAddress(buffer);
    size_t bytesPerRow = CVPixelBufferGetBytesPerRow(buffer);
    CGColorSpaceRef colorSpace = CGColorSpaceCreateDeviceRGB();
    CGContextRef context = CGBitmapContextCreate(
        baseAddress,
        width,
        height,
        8,
        bytesPerRow,
        colorSpace,
        kCGImageAlphaNoneSkipFirst | kCGBitmapByteOrder32Big
    );
    CGColorSpaceRelease(colorSpace);
    if (!context) {
        CVPixelBufferUnlockBaseAddress(buffer, 0);
        CVPixelBufferRelease(buffer);
        return NULL;
    }

    CGRect bounds = CGRectMake(0, 0, width, height);
    CGContextSetRGBFillColor(context, 0.02, 0.02, 0.02, 1);
    CGContextFillRect(context, bounds);
    CGContextSetInterpolationQuality(context, kCGInterpolationHigh);
    CGContextSetAlpha(context, 1);
    CGContextDrawImage(context, bounds, first);
    if (second) {
        CGContextSetAlpha(context, fmin(fmax(progress, 0), 1));
        CGContextDrawImage(context, bounds, second);
    }

    CGContextRelease(context);
    CVPixelBufferUnlockBaseAddress(buffer, 0);
    return buffer;
}

static void WaitUntilReady(AVAssetWriterInput *input) {
    while (!input.readyForMoreMediaData) {
        [NSThread sleepForTimeInterval:0.004];
    }
}

static BOOL RenderMovie(NSArray *scenes, NSURL *outputURL, NSError **renderError) {
    const NSInteger width = 1280;
    const NSInteger height = 720;
    const int32_t fps = 15;
    const double holds[] = {10, 12, 11, 13, 11, 12};
    const double transitionSeconds = 1.0;

    AVAssetWriter *writer = [[AVAssetWriter alloc] initWithURL:outputURL fileType:AVFileTypeQuickTimeMovie error:renderError];
    if (!writer) return NO;
    NSDictionary *settings = @{
        AVVideoCodecKey: AVVideoCodecTypeJPEG,
        AVVideoWidthKey: @(width),
        AVVideoHeightKey: @(height),
        AVVideoCompressionPropertiesKey: @{
            AVVideoQualityKey: @0.88,
        },
    };
    AVAssetWriterInput *input = [AVAssetWriterInput assetWriterInputWithMediaType:AVMediaTypeVideo outputSettings:settings];
    input.expectsMediaDataInRealTime = NO;
    AVAssetWriterInputPixelBufferAdaptor *adaptor = [AVAssetWriterInputPixelBufferAdaptor
        assetWriterInputPixelBufferAdaptorWithAssetWriterInput:input
        sourcePixelBufferAttributes:@{
            (__bridge NSString *)kCVPixelBufferPixelFormatTypeKey: @(kCVPixelFormatType_32ARGB),
            (__bridge NSString *)kCVPixelBufferWidthKey: @(width),
            (__bridge NSString *)kCVPixelBufferHeightKey: @(height),
        }
    ];
    if (![writer canAddInput:input]) return NO;
    [writer addInput:input];
    if (![writer startWriting]) {
        if (renderError) *renderError = writer.error;
        return NO;
    }
    [writer startSessionAtSourceTime:kCMTimeZero];

    __block int64_t frameIndex = 0;
    BOOL (^appendFrame)(CGImageRef, CGImageRef, CGFloat) = ^BOOL(CGImageRef first, CGImageRef second, CGFloat progress) {
        WaitUntilReady(input);
        CVPixelBufferRef buffer = MakePixelBuffer(width, height, first, second, progress);
        if (!buffer) return NO;
        CMTime time = CMTimeMake(frameIndex, fps);
        BOOL ok = [adaptor appendPixelBuffer:buffer withPresentationTime:time];
        CVPixelBufferRelease(buffer);
        if (ok) frameIndex += 1;
        return ok;
    };

    for (NSInteger sceneIndex = 0; sceneIndex < scenes.count; sceneIndex++) {
        CGImageRef current = (__bridge CGImageRef)scenes[sceneIndex];
        NSInteger holdFrames = (NSInteger)llround(holds[sceneIndex] * fps);
        for (NSInteger frame = 0; frame < holdFrames; frame++) {
            @autoreleasepool {
                if (!appendFrame(current, NULL, 0)) goto movie_failed;
            }
        }
        if (sceneIndex < scenes.count - 1) {
            CGImageRef next = (__bridge CGImageRef)scenes[sceneIndex + 1];
            NSInteger transitionFrames = (NSInteger)llround(transitionSeconds * fps);
            for (NSInteger frame = 1; frame <= transitionFrames; frame++) {
                @autoreleasepool {
                    CGFloat progress = (CGFloat)frame / (CGFloat)transitionFrames;
                    if (!appendFrame(current, next, progress)) goto movie_failed;
                }
            }
        }
    }

    [input markAsFinished];
    {
        dispatch_semaphore_t semaphore = dispatch_semaphore_create(0);
        [writer finishWritingWithCompletionHandler:^{ dispatch_semaphore_signal(semaphore); }];
        dispatch_semaphore_wait(semaphore, DISPATCH_TIME_FOREVER);
    }
    if (writer.status != AVAssetWriterStatusCompleted) {
        if (renderError) *renderError = writer.error;
        return NO;
    }
    printf("MOV %s frames=%lld duration=%.1fs\n", outputURL.path.UTF8String, frameIndex, (double)frameIndex / fps);
    return YES;

movie_failed:
    [input markAsFinished];
    [writer cancelWriting];
    if (renderError) *renderError = writer.error;
    return NO;
}

static BOOL AddGIFFrame(
    CGImageDestinationRef destination,
    CGImageRef first,
    CGImageRef second,
    CGFloat progress,
    NSDictionary *properties
) {
    CVPixelBufferRef buffer = MakePixelBuffer(640, 360, first, second, progress);
    if (!buffer) return NO;
    CGImageRef image = NULL;
    OSStatus status = VTCreateCGImageFromCVPixelBuffer(buffer, NULL, &image);
    CVPixelBufferRelease(buffer);
    if (status != noErr || !image) return NO;
    CGImageDestinationAddImage(destination, image, (__bridge CFDictionaryRef)properties);
    CGImageRelease(image);
    return YES;
}

static BOOL RenderGIF(NSArray *scenes, NSURL *outputURL) {
    const NSInteger fps = 6;
    const double holds[] = {3.4, 3.4, 4.2};
    const double transitionSeconds = 0.7;
    NSArray *selected = @[scenes[1], scenes[2], scenes[3]];
    NSInteger frameCount = 0;
    for (NSInteger index = 0; index < selected.count; index++) {
        frameCount += (NSInteger)llround(holds[index] * fps);
        if (index < selected.count - 1) frameCount += (NSInteger)llround(transitionSeconds * fps);
    }

    CGImageDestinationRef destination = CGImageDestinationCreateWithURL(
        (__bridge CFURLRef)outputURL,
        CFSTR("com.compuserve.gif"),
        frameCount,
        NULL
    );
    if (!destination) return NO;
    CGImageDestinationSetProperties(destination, (__bridge CFDictionaryRef)@{
        (__bridge NSString *)kCGImagePropertyGIFDictionary: @{
            (__bridge NSString *)kCGImagePropertyGIFLoopCount: @0,
        },
    });
    NSDictionary *frameProperties = @{
        (__bridge NSString *)kCGImagePropertyGIFDictionary: @{
            (__bridge NSString *)kCGImagePropertyGIFDelayTime: @(1.0 / fps),
        },
    };

    BOOL ok = YES;
    for (NSInteger index = 0; index < selected.count && ok; index++) {
        CGImageRef current = (__bridge CGImageRef)selected[index];
        NSInteger holdFrames = (NSInteger)llround(holds[index] * fps);
        for (NSInteger frame = 0; frame < holdFrames && ok; frame++) {
            ok = AddGIFFrame(destination, current, NULL, 0, frameProperties);
        }
        if (index < selected.count - 1 && ok) {
            CGImageRef next = (__bridge CGImageRef)selected[index + 1];
            NSInteger transitionFrames = (NSInteger)llround(transitionSeconds * fps);
            for (NSInteger frame = 1; frame <= transitionFrames && ok; frame++) {
                CGFloat progress = (CGFloat)frame / (CGFloat)transitionFrames;
                ok = AddGIFFrame(destination, current, next, progress, frameProperties);
            }
        }
    }
    if (ok) ok = CGImageDestinationFinalize(destination);
    CFRelease(destination);
    if (ok) printf("GIF %s frames=%ld duration=%.1fs\n", outputURL.path.UTF8String, (long)frameCount, (double)frameCount / fps);
    return ok;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSFileManager *fileManager = NSFileManager.defaultManager;
        NSURL *projectRoot = [NSURL fileURLWithPath:fileManager.currentDirectoryPath isDirectory:YES];
        NSURL *framesDirectory = [projectRoot URLByAppendingPathComponent:@"docs/demo/qa" isDirectory:YES];
        NSURL *outputDirectory = [projectRoot URLByAppendingPathComponent:@"docs/demo/assets" isDirectory:YES];
        [fileManager createDirectoryAtURL:outputDirectory withIntermediateDirectories:YES attributes:nil error:nil];
        NSURL *movieURL = [NSURL fileURLWithPath:@"/private/tmp/tracejudge_hy3_contest_demo_source.mov"];
        NSURL *gifURL = [outputDirectory URLByAppendingPathComponent:@"tracejudge_hy3_preview.gif"];
        for (NSURL *url in @[movieURL, gifURL]) {
            if ([fileManager fileExistsAtPath:url.path]) [fileManager removeItemAtURL:url error:nil];
        }

        NSMutableArray *scenes = [NSMutableArray array];
        for (NSInteger index = 1; index <= 6; index++) {
            NSString *name = [NSString stringWithFormat:@"contest_scene_%02ld.png", (long)index];
            CGImageRef image = LoadImage([framesDirectory URLByAppendingPathComponent:name]);
            if (!image) {
                fprintf(stderr, "Cannot read %s\n", name.UTF8String);
                return 1;
            }
            [scenes addObject:(__bridge_transfer id)image];
        }

        NSError *error = nil;
        if (!RenderMovie(scenes, movieURL, &error)) {
            fprintf(stderr, "Movie render failed: %s\n", error.localizedDescription.UTF8String);
            return 2;
        }
        if (!RenderGIF(scenes, gifURL)) {
            fprintf(stderr, "GIF render failed\n");
            return 3;
        }
        return 0;
    }
}
