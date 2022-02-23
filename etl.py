import configparser
import pyspark.sql.functions as F
import os
from pyspark.sql import SparkSession
from pyspark.sql.functions import isnan, count, when, col, desc, udf, col, sort_array, asc, avg
from pyspark.sql.functions import sum as Fsum
from pyspark.sql.window import Window
from pyspark.sql.types import IntegerType
from datetime import datetime
from pyspark.sql.functions import monotonically_increasing_id
from pyspark.sql.types import StructType as R, StructField as Fld, DoubleType as Dbl, StringType as Str, IntegerType as Int, LongType as LInt, TimestampType, FloatType as Flt

config = configparser.ConfigParser()
config.read('dl.cfg')

os.environ['AWS_ACCESS_KEY_ID']=config['KEYS']['AWS_ACCESS_KEY_ID']
os.environ['AWS_SECRET_ACCESS_KEY']=config['KEYS']['AWS_SECRET_ACCESS_KEY']

def create_spark_session():
    """
        Description:
            Creates a Spark Session
        
        Arguments:
            None
            
        Returns:
            Spark Session Object
    """
    spark = SparkSession \
        .builder \
        .appName("Data Lake Project") \
        .config("spark.jars.packages", "org.apache.hadoop:hadoop-aws:2.7.0") \
        .getOrCreate()
    spark.sparkContext._jsc.hadoopConfiguration().set("mapreduce.fileoutputcommitter.algorithm.version", "2")
    return spark


def process_song_data(spark, input_data, output_data):
    """
        Description:
            Pipeline that extracts song data from Udacity's S3 bucket, then processes them using Spark, and loads the data back into S3 as a set of dimensional tables (i.e. songs and artists tables). 
        
        Arguments:
            spark: The Spark Session Object
            input_data: Path to Udacity's S3 bucket containing a subset of the Million Song Dataset.
            output_data: Path to S3 bucket where the songs and artists tables are stored in Parquet format
            
        Returns:
            None
    """
    
    # get filepath to song data file
    song_data = os.path.join(input_data, "song_data/A/A/*/*.json")
    
    # read song data file
    songSchema = R([
        Fld("artist_id", Str()),
        Fld("artist_latitude", Dbl()),
        Fld("artist_location", Str()),
        Fld("artist_longitude", Dbl()),
        Fld("artist_name", Str()),
        Fld("duration", Flt()),
        Fld("num_songs", Int()),
        Fld("song_id", Str()),
        Fld("title", Str()),
        Fld("year", Int())
    ])
    df = spark.read.json(song_data, schema=songSchema, multiLine=True, mode='PERMISSIVE', columnNameOfCorruptRecord='corrupt_record')
    df = df.dropDuplicates()
    
    # extract columns to create songs table
    songs_table = df.select(['song_id', 'title', 'artist_id', 'year', 'duration'])
    songs_table = songs_table.dropDuplicates()
    
    # write songs table to parquet files partitioned by year and artist
    songs_table.write.mode("overwrite").parquet(os.path.join(output_data, 'songs'), partitionBy=['year', 'artist_id'])

    # extract columns to create artists table
    fields = ['artist_name', 'artist_location', 'artist_latitude', 'artist_longitude']
    exprs = [col + ' as ' + col.replace('artist_', '') for col in fields]
    
    artist_table = df.selectExpr('artist_id', *exprs)
    
    # write artists table to parquet files
    artist_table.write.mode("overwrite").parquet(os.path.join(output_data, 'artists'))

    # Create a view to aid with creating the songplays files
    df.createOrReplaceTempView('song_df_table')
    
def process_log_data(spark, input_data, output_data):
    """
        Description:
            Pipeline that extracts song data from Udacity's S3 bucket, then processes them using Spark, and loads the data back into S3 as a set of dimensional/fact tables (i.e. users, time and song plays tables). 
        
        Arguments:
            spark: The Spark Session Object
            input_data: Path to  Udacity's S3 bucket consists of log files in JSON format generated by event simulator based on the songs in the dataset above.
            output_data: Path to S3 bucket storing the users, time and song plays tables
            
        Returns:
            None
    """
    
    # get filepath to log data file
    log_data = os.path.join(input_data, "log_data/*/*/*.json")
    
    # read log data file
    df = spark.read.json(log_data)
    
    # filter by actions for song plays
    df = df.where(df.page == 'NextSong')
    df = df.dropDuplicates()
    
    # extract columns for users table    
    df = df.withColumn('user_id', df.userId.cast(Int()))
    users_table = df.selectExpr(['user_id', 'firstName as first_name', 'lastName as last_name', 'gender', 'level', 'ts'])
    
    # write users table to parquet files
    users_window = Window.partitionBy('user_id').orderBy(F.desc('ts'))
    users_table = users_table.withColumn('row_number', F.row_number().over(users_window))
    users_table = users_table.where(users_table.row_number == 1).drop('ts', 'row_number') 
    users_table.write.mode("overwrite").parquet(os.path.join(output_data, 'users'))
    
    # create timestamp column from original timestamp column
    get_timestamp = udf(lambda ts: datetime.fromtimestamp(ts/1000).isoformat())
    df = df.withColumn('start_time', get_timestamp('ts').cast(TimestampType()))

    # create datetime column from original timestamp column    
    # extract columns to create time table
    time_table = df.select('start_time').dropDuplicates() \
    .withColumn('hour', F.hour('start_time')) \
    .withColumn('day', F.dayofmonth('start_time')) \
    .withColumn('week', F.weekofyear('start_time')) \
    .withColumn('month', F.month('start_time')) \
    .withColumn('year', F.year('start_time')) \
    .withColumn('weekday', F.dayofweek('start_time'))
    
    # write time table to parquet files partitioned by year and month
    time_table.write.mode("overwrite").parquet(os.path.join(output_data, 'time'), partitionBy=['year', 'month'])

    # read in song data to use for songplays table
    song_df = spark.sql('SELECT DISTINCT song_id, title, artist_id, artist_name, duration FROM song_df_table')

    # extract columns from joined song and log datasets to create songplays table 
    songplays_table = df.join(song_df, (df.song == song_df.title) & (df.artist == song_df.artist_name) & (df.length == song_df.duration), 'left_outer') \
        .distinct() \
        .select(monotonically_increasing_id().alias("songplay_id"),
                 col("start_time"),
                 col("user_id"),
                 col("level"),
                 col("song_id"),
                 col("artist_id"),
                 col("sessionId").alias('session_id'),
                 col("location"),
                 col("userAgent").alias("user_agent")
        ).withColumn("month", F.month(col("start_time"))) \
         .withColumn("year", F.year(col("start_time")))

    # write songplays table to parquet files partitioned by year and month
    songplays_table.write.mode("overwrite").parquet(os.path.join(output_data, 'songplays'), partitionBy=['year', 'month'])


def main():
    """
        Description:
            Extract song and log data from Udacity's S3 bucket, transform into Fact/Dimension tables and generate a Parquet format stored in destination's S3 bucket 
        
        Arguments:
            None
            
        Returns:
            None
    """
        
    spark = create_spark_session()
    input_data = "s3a://udacity-dend/"
    output_data = "s3a://my-datalake-bucket-test/"
    
    process_song_data(spark, input_data, output_data)    
    process_log_data(spark, input_data, output_data)


if __name__ == "__main__":
    main()
